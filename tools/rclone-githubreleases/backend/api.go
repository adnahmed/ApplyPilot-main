package githubreleases

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	apiHost          = "api.github.com"
	uploadHost       = "uploads.github.com"
	mutationInterval = 9 * time.Second
)

type apiError struct{ status int }

func (e *apiError) Error() string { return fmt.Sprintf("GitHub API returned HTTP %d", e.status) }
func hasStatus(err error, status int) bool {
	var e *apiError
	return errors.As(err, &e) && e.status == status
}

type githubAPI struct {
	repository, token          string
	client                     *http.Client
	mu                         sync.Mutex
	nextMutation, blockedUntil time.Time
}

func newAPI(repository, token string) *githubAPI {
	return &githubAPI{repository: repository, token: token, client: &http.Client{
		Timeout: 30 * time.Minute,
		CheckRedirect: func(req *http.Request, via []*http.Request) error {
			if len(via) >= 5 {
				return errors.New("too many GitHub asset redirects")
			}
			host := req.URL.Hostname()
			if req.URL.Scheme != "https" || req.URL.Port() != "" || (host != "release-assets.githubusercontent.com" && host != "objects.githubusercontent.com") {
				return errors.New("GitHub asset redirected outside the allowed download hosts")
			}
			req.Header.Del("Authorization")
			req.Header.Del("X-GitHub-Api-Version")
			return nil
		},
	}}
}

func pause(ctx context.Context, delay time.Duration) error {
	if delay <= 0 {
		return nil
	}
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

func (a *githubAPI) wait(ctx context.Context, mutation bool) error {
	for {
		a.mu.Lock()
		until := a.blockedUntil
		if mutation && a.nextMutation.After(until) {
			until = a.nextMutation
		}
		if !until.After(time.Now()) {
			if mutation {
				a.nextMutation = time.Now().Add(mutationInterval)
			}
			a.mu.Unlock()
			return nil
		}
		a.mu.Unlock()
		if err := pause(ctx, time.Until(until)); err != nil {
			return err
		}
	}
}

func (a *githubAPI) rateLimit(header http.Header, status int, attempt int) time.Duration {
	delay := time.Duration(1<<attempt) * time.Second
	limited := status == 429 || (status == 403 && (header.Get("Retry-After") != "" || header.Get("X-RateLimit-Remaining") == "0"))
	if limited {
		delay = time.Minute
	}
	if seconds, err := strconv.Atoi(header.Get("Retry-After")); err == nil {
		delay = max(delay, time.Duration(seconds)*time.Second)
	} else if date, err := http.ParseTime(header.Get("Retry-After")); err == nil {
		delay = max(delay, time.Until(date))
	}
	if header.Get("X-RateLimit-Remaining") == "0" {
		if reset, err := strconv.ParseInt(header.Get("X-RateLimit-Reset"), 10, 64); err == nil {
			delay = max(delay, time.Until(time.Unix(reset+1, 0)))
		}
		limited = true
	}
	if limited {
		a.mu.Lock()
		a.blockedUntil = maxTime(a.blockedUntil, time.Now().Add(delay))
		a.mu.Unlock()
	}
	return delay
}

func maxTime(a, b time.Time) time.Time {
	if a.After(b) {
		return a
	}
	return b
}

// POST is reconciled by its caller instead of blindly replaying an ambiguous write.
func (a *githubAPI) request(ctx context.Context, method, endpoint, accept string, body io.ReadSeeker, size int64, headers http.Header) (*http.Response, error) {
	u, err := url.Parse(endpoint)
	if err != nil || u.Scheme != "https" || (u.Host != apiHost && u.Host != uploadHost) || u.User != nil {
		return nil, errors.New("invalid GitHub API endpoint")
	}
	mutation := method != http.MethodGet && method != http.MethodHead
	for attempt := 0; attempt < 6; attempt++ {
		if err := a.wait(ctx, mutation); err != nil {
			return nil, err
		}
		if body != nil {
			if _, err := body.Seek(0, io.SeekStart); err != nil {
				return nil, err
			}
		}
		var reader io.Reader
		if body != nil {
			reader = struct{ io.Reader }{body}
		}
		req, err := http.NewRequestWithContext(ctx, method, endpoint, reader)
		if err != nil {
			return nil, errors.New("cannot create GitHub API request")
		}
		req.ContentLength = size
		req.Header.Set("Authorization", "Bearer "+a.token)
		req.Header.Set("X-GitHub-Api-Version", "2022-11-28")
		req.Header.Set("User-Agent", "ApplyPilot-rclone-githubreleases/1")
		req.Header.Set("Accept", accept)
		for k, values := range headers {
			req.Header[k] = values
		}
		response, err := a.client.Do(req)
		if err != nil {
			if ctx.Err() != nil {
				return nil, ctx.Err()
			}
			if method == http.MethodPost || attempt == 5 {
				return nil, errors.New("GitHub request failed; the remote outcome may be unknown")
			}
			if err := pause(ctx, time.Duration(1<<attempt)*time.Second); err != nil {
				return nil, err
			}
			continue
		}
		delay := a.rateLimit(response.Header, response.StatusCode, attempt)
		if response.StatusCode >= 200 && response.StatusCode < 300 {
			return response, nil
		}
		io.Copy(io.Discard, io.LimitReader(response.Body, 8192))
		response.Body.Close()
		err = &apiError{status: response.StatusCode}
		limited := response.StatusCode == 429 || (response.StatusCode == 403 && (response.Header.Get("Retry-After") != "" || response.Header.Get("X-RateLimit-Remaining") == "0"))
		retry := limited || (response.StatusCode >= 500 && method != http.MethodPost)
		if !retry || attempt == 5 {
			return nil, err
		}
		if err := pause(ctx, delay); err != nil {
			return nil, err
		}
	}
	return nil, errors.New("GitHub request retries exhausted")
}

func (a *githubAPI) endpoint(suffix string) string {
	return "https://" + apiHost + "/repos/" + a.repository + suffix
}

func (a *githubAPI) json(ctx context.Context, method, suffix string, input, output any) error {
	var body *bytes.Reader
	var size int64
	if input != nil {
		encoded, err := json.Marshal(input)
		if err != nil {
			return err
		}
		body = bytes.NewReader(encoded)
		size = int64(len(encoded))
	}
	var reader io.ReadSeeker
	if body != nil {
		reader = body
	}
	response, err := a.request(ctx, method, a.endpoint(suffix), "application/vnd.github+json", reader, size, http.Header{"Content-Type": {"application/json"}})
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if output == nil {
		_, err = io.Copy(io.Discard, response.Body)
		return err
	}
	if err := json.NewDecoder(response.Body).Decode(output); err != nil {
		return errors.New("invalid GitHub API JSON response")
	}
	return nil
}

func (a *githubAPI) privateRepository(ctx context.Context) error {
	var repo struct {
		Private  bool `json:"private"`
		Archived bool `json:"archived"`
	}
	if err := a.json(ctx, http.MethodGet, "", nil, &repo); err != nil {
		return err
	}
	if !repo.Private {
		return errors.New("GitHub Releases backups require a private repository")
	}
	if repo.Archived {
		return errors.New("GitHub Releases backup repository is archived")
	}
	return nil
}

func safePath(value string) bool {
	if value == "" || strings.HasPrefix(value, "/") || strings.ContainsAny(value, "\\\x00") {
		return false
	}
	for _, part := range strings.Split(value, "/") {
		if part == "" || part == "." || part == ".." {
			return false
		}
	}
	return true
}
