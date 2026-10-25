# Primary sources inspected for this patch

Inspected during this delivery on 2026-09-28. Inspection confirms documentation or
published source/version metadata; it does not substitute for installing or
running the dependency. The delivery environment could browse documentation but
could not download packages into the execution container.

| Source | What it informed |
|---|---|
| https://docs.temporal.io/guides | Temporal documentation entry point requested in the brief. |
| https://github.com/temporalio/sdk-python | Official Python SDK worker/workflow conventions. |
| https://docs.temporal.io/develop/python/workflows/child-workflows | Awaiting child start and independent child close policy. |
| https://docs.temporal.io/develop/python/best-practices/error-handling | Activity retries, non-retryable failures and side-effect limitations. |
| https://python.temporal.io/temporalio.exceptions.ApplicationError.html | `next_retry_delay` and non-retryable error properties. |
| https://docs.temporal.io/develop/python/workflows/schedules | Native schedules; existing generic adapters do not become semantic migrations merely by scheduling them. |
| https://docs.temporal.io/develop/python/workflows/continue-as-new | Native history continuation; no global work-volume cap added. |
| https://pypi.org/pypi/temporalio/1.33.0/json | Existing repository SDK pin is a published package; installation was not possible here. |
| https://raw.githubusercontent.com/stanfordnlp/dspy/3.3.1/dspy/teleprompt/mipro_optimizer_v2.py | Public MIPROv2 constructor/compile parameters; removal of private optimizer overrides. |
| https://raw.githubusercontent.com/deedy5/ddgs/v9.5.5/ddgs/ddgs.py | Public DDGS text/news, page, max_results and backend API. |
| https://raw.githubusercontent.com/deedy5/ddgs/v9.5.5/ddgs/exceptions.py | Actual DDGS exception classes. |
| https://pypi.org/pypi/ddgs/9.5.5/json | Published DDGS pin for the proposed search image. |
| https://pypi.org/pypi/psycopg/3.2.12/json | Published PostgreSQL driver pin for the proposed search image. |
| https://pypi.org/pypi/psycopg-pool/3.2.7/json | Published pool pin for the proposed search image. |
| https://docs.searxng.org/dev/search_api.html | JSON search endpoint, query/category/page parameters. |

Also inspected the requested Linked Helper, Prospeo, Darkzwarmer, XActions and
LiteLLM references. They did not establish that this repository already implements
safe email warm-up, Cloudflare sender provisioning, XActions publishing or a
corrected LiteLLM scheduler configuration. Those integrations were not claimed
implemented or tested in this patch. No third-party automation code was copied
into the domain layer.

Requested contextual references:

- https://www.linkedhelper.com/blog/linkedin-automation-limits
- https://prospeo.io/s/gmail-warm-up
- https://github.com/darkzOGx/darkzwarmer
- https://github.com/nirholas/XActions
- https://docs.litellm.ai/docs/proxy/config_settings
