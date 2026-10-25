// Package githubreleases stores restic objects in private GitHub draft releases.
package githubreleases

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/rclone/rclone/backend/overview"
	"github.com/rclone/rclone/fs"
	"github.com/rclone/rclone/fs/config/configmap"
	"github.com/rclone/rclone/fs/config/configstruct"
	"github.com/rclone/rclone/fs/hash"
)

const (
	shardCapacity       = 900
	maximumSize   int64 = 2 << 30
	assetPrefix         = "ap1-"
)

type options struct {
	Repository    string `config:"repository"`
	Token         string `config:"token"`
	ReleasePrefix string `config:"release_prefix"`
}

type release struct {
	ID        int64  `json:"id"`
	Tag       string `json:"tag_name"`
	Body      string `json:"body"`
	Draft     bool   `json:"draft"`
	Immutable bool   `json:"immutable"`
	count     int
}

type asset struct {
	ID        int64     `json:"id"`
	Name      string    `json:"name"`
	Size      int64     `json:"size"`
	State     string    `json:"state"`
	Digest    string    `json:"digest"`
	Created   time.Time `json:"created_at"`
	releaseID int64
}

type Fs struct {
	name, root string
	opt        options
	api        *githubAPI
	features   *fs.Features
	mu         sync.Mutex
	releases   map[int64]*release
	objects    map[string][]asset
	loaded     bool
	dataStale  bool
}

type Object struct {
	fs     *Fs
	remote string
	asset  asset
}

func init() {
	// rclone 1.75 Register requires embedded in-tree overview YAML. Its public
	// registry also accepts external entries with explicit defaults and overview.
	fs.Registry = append(fs.Registry, &fs.RegInfo{
		Name: "githubreleases", Prefix: "githubreleases", Description: "Private GitHub Releases (ApplyPilot restic objects)", NewFs: NewFs,
		Overview: &overview.BackendConfig{Backend: "githubreleases", Name: "Private GitHub Releases", Hashes: []string{"SHA-256"}},
		Options: []fs.Option{
			{Name: "repository", Default: "", Help: "Private GitHub repository in owner/name form.", Required: true},
			{Name: "token", Default: "", Help: "Raw GitHub token with Contents read/write permission. Supply through the environment.", Required: true, IsPassword: true},
			{Name: "release_prefix", Default: "", Help: "Workspace-specific release namespace, e.g. applypilot-store-a1b2c3d4.", Required: true},
		},
	})
}

func NewFs(ctx context.Context, name, root string, mapper configmap.Mapper) (fs.Fs, error) {
	f := &Fs{name: name, root: strings.Trim(root, "/"), releases: make(map[int64]*release), objects: make(map[string][]asset)}
	if err := configstruct.Set(mapper, &f.opt); err != nil {
		return nil, err
	}
	if !regexp.MustCompile(`^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$`).MatchString(f.opt.Repository) || strings.Contains(f.opt.Repository, "..") {
		return nil, errors.New("repository must be owner/name")
	}
	if !regexp.MustCompile(`^applypilot-store-[a-zA-Z0-9-]{4,80}$`).MatchString(f.opt.ReleasePrefix) {
		return nil, errors.New("release_prefix must be a workspace-specific applypilot-store- namespace")
	}
	if f.opt.Token == "" {
		return nil, errors.New("GitHub token is required")
	}
	if f.root != "" && !safePath(f.root) {
		return nil, errors.New("invalid remote root")
	}
	f.api = newAPI(f.opt.Repository, f.opt.Token)
	if err := f.api.privateRepository(ctx); err != nil {
		return nil, err
	}
	f.features = (&fs.Features{}).Fill(ctx, f)
	f.features.PutStream = f.Put
	return f, nil
}

func (f *Fs) Name() string             { return f.name }
func (f *Fs) Root() string             { return f.root }
func (f *Fs) String() string           { return "GitHub Releases " + f.opt.Repository + ":" + f.root }
func (f *Fs) Precision() time.Duration { return fs.ModTimeNotSupported }
func (f *Fs) Hashes() hash.Set         { return hash.NewHashSet(hash.SHA256) }
func (f *Fs) Features() *fs.Features   { return f.features }
func (f *Fs) marker() string {
	return "ApplyPilot restic object storage v1\nnamespace=" + f.opt.ReleasePrefix
}
func (f *Fs) full(remote string) string { return path.Join(f.root, remote) }
func (f *Fs) lockPath(full string) bool { return strings.HasPrefix(full, f.full("locks")+"/") }

func (f *Fs) owned(r *release) bool {
	if !strings.HasPrefix(r.Tag, f.opt.ReleasePrefix+"-") {
		return false
	}
	suffix := strings.TrimPrefix(r.Tag, f.opt.ReleasePrefix+"-")
	return suffix == "locks" || suffix == "control" || regexp.MustCompile(`^[0-9]{6}$`).MatchString(suffix)
}

func (f *Fs) validateRelease(r *release) error {
	if !f.owned(r) || r.Body != f.marker() {
		return errors.New("release namespace collision: refusing to touch an unowned release")
	}
	if !r.Draft || r.Immutable {
		return errors.New("backup shards must remain mutable draft releases; refusing to modify a published or immutable shard")
	}
	return nil
}

func (f *Fs) listReleases(ctx context.Context) (map[int64]*release, error) {
	result := make(map[int64]*release)
	for page := 1; ; page++ {
		var batch []*release
		if err := f.api.json(ctx, http.MethodGet, fmt.Sprintf("/releases?per_page=100&page=%d", page), nil, &batch); err != nil {
			return nil, err
		}
		for _, r := range batch {
			if !f.owned(r) {
				continue
			}
			if err := f.validateRelease(r); err != nil {
				return nil, err
			}
			if previous := result[r.ID]; previous != nil {
				if previous.Tag != r.Tag || previous.Body != r.Body || previous.Draft != r.Draft || previous.Immutable != r.Immutable {
					return nil, errors.New("backup shard metadata changed during release listing; retry inventory")
				}
				continue
			}
			// Interrupted initializations can leave several owned drafts with the
			// same tag. Index every asset; object() rejects conflicting contents.
			result[r.ID] = r
		}
		if len(batch) < 100 {
			return result, nil
		}
	}
}

func (f *Fs) listAssets(ctx context.Context, r *release) ([]asset, error) {
	var result []asset
	for page := 1; ; page++ {
		var batch []asset
		if err := f.api.json(ctx, http.MethodGet, fmt.Sprintf("/releases/%d/assets?per_page=100&page=%d", r.ID, page), nil, &batch); err != nil {
			return nil, err
		}
		for _, a := range batch {
			a.releaseID = r.ID
			result = append(result, a)
		}
		if len(batch) < 100 {
			r.count = len(result)
			return result, nil
		}
	}
}

func assetName(full string) string {
	return assetPrefix + base64.RawURLEncoding.EncodeToString([]byte(full))
}
func assetPath(name string) (string, bool) {
	if !strings.HasPrefix(name, assetPrefix) {
		return "", false
	}
	decoded, err := base64.RawURLEncoding.DecodeString(strings.TrimPrefix(name, assetPrefix))
	value := string(decoded)
	return value, err == nil && safePath(value) && assetName(value) == name
}

// Locks are re-listed on every lookup, including discovery of concurrently created shards.
func (f *Fs) refresh(ctx context.Context, locksOnly bool) error {
	if !f.loaded {
		locksOnly = false
	}
	releases, err := f.listReleases(ctx)
	if err != nil {
		return err
	}
	index := make(map[string][]asset)
	if locksOnly {
		for full, objects := range f.objects {
			if !f.lockPath(full) {
				index[full] = objects
			}
		}
	}
	for id, r := range releases {
		if locksOnly && r.Tag != f.opt.ReleasePrefix+"-locks" {
			r.count = -1
			if previous := f.releases[id]; previous != nil {
				r.count = previous.count
			}
			continue
		}
		assets, err := f.listAssets(ctx, r)
		if err != nil {
			return err
		}
		for _, a := range assets {
			full, ok := assetPath(a.Name)
			if !ok || a.State != "uploaded" {
				continue
			}
			if f.lockPath(full) != (r.Tag == f.opt.ReleasePrefix+"-locks") {
				return errors.New("restic lock object found outside its dedicated shard")
			}
			index[full] = append(index[full], a)
		}
	}
	f.releases, f.objects, f.loaded = releases, index, true
	f.dataStale = locksOnly
	return nil
}

func (f *Fs) ensure(ctx context.Context, remote string) error {
	if !f.loaded || remote == "locks" || strings.HasPrefix(remote, "locks/") {
		return f.refresh(ctx, f.loaded)
	}
	// A lock may have been acquired after another writer committed. Read the
	// inventory again before using its view of snapshots, indexes or packs.
	if f.dataStale {
		return f.refresh(ctx, false)
	}
	return nil
}

func (f *Fs) object(ctx context.Context, remote string) (*Object, error) {
	assets := f.objects[f.full(remote)]
	if len(assets) == 0 {
		return nil, fs.ErrorObjectNotFound
	}
	first := assets[0]
	if len(assets) > 1 {
		digest, err := f.digest(ctx, first)
		if err != nil {
			return nil, err
		}
		for _, other := range assets[1:] {
			otherDigest, err := f.digest(ctx, other)
			if err != nil {
				return nil, err
			}
			if other.Size != first.Size || digest != otherDigest {
				return nil, errors.New("conflicting copies of immutable restic object")
			}
		}
	}
	return &Object{fs: f, remote: remote, asset: first}, nil
}

func (f *Fs) NewObject(ctx context.Context, remote string) (fs.Object, error) {
	if !safePath(remote) {
		return nil, fs.ErrorObjectNotFound
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if err := f.ensure(ctx, remote); err != nil {
		fs.Errorf(f, "Cannot read backup object inventory: %v", err)
		return nil, err
	}
	object, err := f.object(ctx, remote)
	if err != nil && !errors.Is(err, fs.ErrorObjectNotFound) {
		fs.Errorf(f, "Cannot read backup object %q: %v", remote, err)
	}
	return object, err
}

func (f *Fs) List(ctx context.Context, dir string) (fs.DirEntries, error) {
	if dir != "" && !safePath(dir) {
		return nil, fs.ErrorDirNotFound
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if err := f.ensure(ctx, dir); err != nil {
		return nil, err
	}
	prefix := f.full(dir)
	if prefix == "." {
		prefix = ""
	}
	if prefix != "" {
		prefix += "/"
	}
	var entries fs.DirEntries
	dirs := make(map[string]bool)
	for full := range f.objects {
		if !strings.HasPrefix(full, prefix) {
			continue
		}
		remainder := strings.TrimPrefix(full, prefix)
		first, _, nested := strings.Cut(remainder, "/")
		if nested {
			dirs[path.Join(dir, first)] = true
			continue
		}
		remote := path.Join(dir, first)
		obj, err := f.object(ctx, remote)
		if err != nil {
			return nil, err
		}
		entries = append(entries, obj)
	}
	if dir == "" {
		for _, name := range []string{"data", "index", "keys", "locks", "snapshots"} {
			dirs[name] = true
		}
	}
	for name := range dirs {
		entries = append(entries, fs.NewDir(name, time.Time{}))
	}
	return entries, nil
}

func (f *Fs) Mkdir(_ context.Context, dir string) error {
	if dir != "" && !safePath(dir) {
		return errors.New("invalid directory")
	}
	return nil
}
func (f *Fs) Rmdir(ctx context.Context, dir string) error {
	entries, err := f.List(ctx, dir)
	if err != nil {
		return err
	}
	if len(entries) > 0 {
		return fs.ErrorDirectoryNotEmpty
	}
	return nil
}

func (f *Fs) writableRelease(ctx context.Context, id int64) (*release, error) {
	var r release
	if err := f.api.json(ctx, http.MethodGet, fmt.Sprintf("/releases/%d", id), nil, &r); err != nil {
		return nil, err
	}
	if err := f.validateRelease(&r); err != nil {
		return nil, err
	}
	return &r, nil
}

func releaseForTag(releases map[int64]*release, tag string) *release {
	var selected *release
	for _, r := range releases {
		if r.Tag == tag && (selected == nil || r.ID < selected.ID) {
			selected = r
		}
	}
	return selected
}

func (f *Fs) createRelease(ctx context.Context, tag string) (*release, error) {
	// A prior invocation may have created the draft before its response was lost.
	releases, listErr := f.listReleases(ctx)
	if listErr != nil {
		return nil, listErr
	}
	if r := releaseForTag(releases, tag); r != nil {
		f.releases[r.ID] = r
		return r, nil
	}
	var created release
	err := f.api.json(ctx, http.MethodPost, "/releases", map[string]any{"tag_name": tag, "name": tag, "body": f.marker(), "draft": true}, &created)
	// A failed POST may already have created the draft. Discover it before retrying.
	releases, listErr = f.listReleases(ctx)
	if listErr != nil {
		return nil, listErr
	}
	if r := releaseForTag(releases, tag); r != nil {
		f.releases[r.ID] = r
		return r, nil
	}
	if err != nil {
		return nil, err
	}
	return nil, errors.New("created backup shard is not visible in release listing")
}

func (f *Fs) chooseRelease(ctx context.Context, remote string) (*release, error) {
	tag := ""
	if strings.HasPrefix(remote, "locks/") {
		tag = f.opt.ReleasePrefix + "-locks"
	}
	if remote == "config" {
		tag = f.opt.ReleasePrefix + "-control"
	}
	if tag != "" {
		if r := releaseForTag(f.releases, tag); r != nil {
			return r, nil
		}
		return f.createRelease(ctx, tag)
	}
	var candidates []*release
	maxShard := 0
	for _, r := range f.releases {
		suffix := strings.TrimPrefix(r.Tag, f.opt.ReleasePrefix+"-")
		if number, err := strconv.Atoi(suffix); err == nil {
			maxShard = max(maxShard, number)
			if r.count >= 0 && r.count < shardCapacity {
				candidates = append(candidates, r)
			}
		}
	}
	sort.Slice(candidates, func(i, j int) bool {
		if candidates[i].Tag == candidates[j].Tag {
			return candidates[i].ID < candidates[j].ID
		}
		return candidates[i].Tag < candidates[j].Tag
	})
	if len(candidates) > 0 {
		return candidates[0], nil
	}
	if maxShard >= 999999 {
		return nil, errors.New("backup release shard namespace exhausted")
	}
	return f.createRelease(ctx, fmt.Sprintf("%s-%06d", f.opt.ReleasePrefix, maxShard+1))
}

func (f *Fs) digest(ctx context.Context, a asset) (string, error) {
	if strings.HasPrefix(a.Digest, "sha256:") && len(a.Digest) == 71 {
		return strings.TrimPrefix(a.Digest, "sha256:"), nil
	}
	body, err := f.openAsset(ctx, a, nil)
	if err != nil {
		return "", err
	}
	defer body.Close()
	h := sha256.New()
	if _, err := io.Copy(h, body); err != nil {
		return "", errors.New("cannot read GitHub object for integrity verification")
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

func (f *Fs) Put(ctx context.Context, in io.Reader, src fs.ObjectInfo, _ ...fs.OpenOption) (fs.Object, error) {
	remote := src.Remote()
	if !safePath(remote) || len(assetName(f.full(remote))) > 240 {
		return nil, errors.New("invalid or excessively long restic object path")
	}
	if src.Size() >= maximumSize {
		return nil, errors.New("GitHub release assets must be smaller than 2 GiB")
	}
	file, err := os.CreateTemp("", "applypilot-restic-object-*")
	if err != nil {
		return nil, err
	}
	defer func() { file.Close(); os.Remove(file.Name()) }()
	digest := sha256.New()
	size, err := io.Copy(io.MultiWriter(file, digest), io.LimitReader(in, maximumSize))
	if err != nil {
		return nil, errors.New("cannot stage restic object")
	}
	if size >= maximumSize {
		return nil, errors.New("GitHub release assets must be smaller than 2 GiB")
	}
	if src.Size() >= 0 && src.Size() != size {
		return nil, errors.New("restic object size does not match upload")
	}
	wantDigest := hex.EncodeToString(digest.Sum(nil))
	f.mu.Lock()
	defer f.mu.Unlock()
	if err := f.ensure(ctx, remote); err != nil {
		return nil, err
	}
	if existing, err := f.object(ctx, remote); err == nil {
		return f.sameObject(ctx, existing, wantDigest, size)
	} else if !errors.Is(err, fs.ErrorObjectNotFound) {
		return nil, err
	}
	for attempt := 0; attempt < 3; attempt++ {
		r, err := f.chooseRelease(ctx, remote)
		if err != nil {
			return nil, err
		}
		if _, err := f.writableRelease(ctx, r.ID); err != nil {
			return nil, err
		}
		if r.count >= shardCapacity {
			return nil, errors.New("backup control or lock shard is full")
		}
		endpoint := fmt.Sprintf("https://%s/repos/%s/releases/%d/assets?name=%s", uploadHost, f.opt.Repository, r.ID, url.QueryEscape(assetName(f.full(remote))))
		response, uploadErr := f.api.request(ctx, http.MethodPost, endpoint, "application/vnd.github+json", file, size, http.Header{"Content-Type": {"application/octet-stream"}})
		if response != nil {
			var uploaded asset
			decodeErr := json.NewDecoder(io.LimitReader(response.Body, 1<<20)).Decode(&uploaded)
			response.Body.Close()
			if decodeErr == nil && uploaded.ID > 0 && uploaded.Name == assetName(f.full(remote)) && uploaded.State == "uploaded" {
				uploaded.releaseID = r.ID
				f.objects[f.full(remote)] = append(f.objects[f.full(remote)], uploaded)
				r.count++
				return f.sameObject(ctx, &Object{fs: f, remote: remote, asset: uploaded}, wantDigest, size)
			}
		}
		// Reconcile only ambiguous uploads; successful writes update the local index directly.
		assets, err := f.listAssets(ctx, r)
		if err != nil {
			return nil, err
		}
		for _, a := range assets {
			if a.Name != assetName(f.full(remote)) {
				continue
			}
			if a.State == "starter" {
				// Active uploads are capped at 30 minutes by the HTTP client.
				if a.Size == 0 && !a.Created.IsZero() && time.Since(a.Created) > time.Hour {
					if _, err := f.writableRelease(ctx, r.ID); err != nil {
						return nil, err
					}
					if err := f.api.json(ctx, http.MethodDelete, fmt.Sprintf("/releases/assets/%d", a.ID), nil, nil); err != nil && !hasStatus(err, 404) {
						return nil, err
					}
					continue
				}
				return nil, errors.New("incomplete GitHub upload occupies this object name; retry after its one-hour recovery window")
			}
			if a.State != "uploaded" {
				continue
			}
			f.objects[f.full(remote)] = append(f.objects[f.full(remote)], a)
			return f.sameObject(ctx, &Object{fs: f, remote: remote, asset: a}, wantDigest, size)
		}
		var apiErr *apiError
		if errors.As(uploadErr, &apiErr) && apiErr.status != 422 && apiErr.status < 500 {
			return nil, uploadErr
		}
		if err := f.refresh(ctx, false); err != nil {
			return nil, err
		}
		if existing, err := f.object(ctx, remote); err == nil {
			return f.sameObject(ctx, existing, wantDigest, size)
		} else if !errors.Is(err, fs.ErrorObjectNotFound) {
			return nil, err
		}
	}
	return nil, errors.New("GitHub upload was not visible after reconciliation")
}

func (f *Fs) sameObject(ctx context.Context, object *Object, digest string, size int64) (fs.Object, error) {
	actual, err := f.digest(ctx, object.asset)
	if err != nil {
		return nil, err
	}
	if actual != digest || object.Size() != size {
		return nil, errors.New("refusing to overwrite immutable restic object with different bytes")
	}
	return object, nil
}

type limitedCloser struct {
	io.Reader
	io.Closer
}

func (f *Fs) openAsset(ctx context.Context, a asset, options []fs.OpenOption) (io.ReadCloser, error) {
	fs.FixRangeOption(options, a.Size)
	headers := make(http.Header)
	var offset, limit int64 = 0, -1
	for _, option := range options {
		if r, ok := option.(*fs.RangeOption); ok {
			offset, limit = r.Decode(a.Size)
			key, value := r.Header()
			headers.Set(key, value)
		} else if option.Mandatory() {
			return nil, errors.New("unsupported mandatory download option")
		}
	}
	response, err := f.api.request(ctx, http.MethodGet, f.api.endpoint(fmt.Sprintf("/releases/assets/%d", a.ID)), "application/octet-stream", nil, 0, headers)
	if hasStatus(err, 404) {
		return nil, fs.ErrorObjectNotFound
	}
	if err != nil {
		return nil, err
	}
	if response.StatusCode == http.StatusPartialContent && headers.Get("Range") != "" {
		if !strings.HasPrefix(response.Header.Get("Content-Range"), fmt.Sprintf("bytes %d-", offset)) {
			response.Body.Close()
			return nil, errors.New("GitHub asset returned an unexpected byte range")
		}
	} else if offset > 0 {
		if _, err := io.CopyN(io.Discard, response.Body, offset); err != nil {
			response.Body.Close()
			return nil, errors.New("GitHub asset is shorter than the requested range")
		}
	}
	if limit >= 0 {
		return &limitedCloser{Reader: io.LimitReader(response.Body, limit), Closer: response.Body}, nil
	}
	return response.Body, nil
}

func (o *Object) Fs() fs.Info                       { return o.fs }
func (o *Object) String() string                    { return o.remote }
func (o *Object) Remote() string                    { return o.remote }
func (o *Object) ModTime(context.Context) time.Time { return o.asset.Created }
func (o *Object) Size() int64                       { return o.asset.Size }
func (o *Object) Storable() bool                    { return true }
func (o *Object) Hash(ctx context.Context, kind hash.Type) (string, error) {
	if kind != hash.SHA256 {
		return "", hash.ErrUnsupported
	}
	return o.fs.digest(ctx, o.asset)
}
func (o *Object) SetModTime(context.Context, time.Time) error { return fs.ErrorCantSetModTime }
func (o *Object) Open(ctx context.Context, options ...fs.OpenOption) (io.ReadCloser, error) {
	return o.fs.openAsset(ctx, o.asset, options)
}
func (o *Object) Update(ctx context.Context, in io.Reader, src fs.ObjectInfo, options ...fs.OpenOption) error {
	object, err := o.fs.Put(ctx, in, src, options...)
	if err == nil {
		o.asset = object.(*Object).asset
	}
	return err
}

func (o *Object) Remove(ctx context.Context) error {
	f := o.fs
	f.mu.Lock()
	defer f.mu.Unlock()
	if err := f.ensure(ctx, o.remote); err != nil {
		return err
	}
	assets := f.objects[f.full(o.remote)]
	if len(assets) == 0 {
		return nil
	}
	if _, err := f.object(ctx, o.remote); err != nil {
		return err
	}
	for _, a := range assets {
		if _, err := f.writableRelease(ctx, a.releaseID); err != nil {
			return err
		}
		err := f.api.json(ctx, http.MethodDelete, fmt.Sprintf("/releases/assets/%d", a.ID), nil, nil)
		if err != nil && !hasStatus(err, 404) {
			return err
		}
		if r := f.releases[a.releaseID]; r != nil {
			r.count--
		}
	}
	delete(f.objects, f.full(o.remote))
	return nil
}

var _ fs.Fs = (*Fs)(nil)
var _ fs.Object = (*Object)(nil)
