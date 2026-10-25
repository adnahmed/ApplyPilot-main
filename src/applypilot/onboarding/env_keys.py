"""Supported setting names from the uploaded examples; no user values or pricing claims."""
ENV_HINTS = {'AGNES_API_KEY': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                  'limits, pricing, or start the gateway.',
 'AGNES_KEY_1': 'Existing gateway setting. Saving a credential does not change provider permissions, limits, '
                'pricing, or start the gateway.',
 'AGNES_KEY_2': 'Existing gateway setting. Saving a credential does not change provider permissions, limits, '
                'pricing, or start the gateway.',
 'AGNES_KEY_3': 'Existing gateway setting. Saving a credential does not change provider permissions, limits, '
                'pricing, or start the gateway.',
 'AGNES_KEY_4': 'Existing gateway setting. Saving a credential does not change provider permissions, limits, '
                'pricing, or start the gateway.',
 'AGNES_KEY_5': 'Existing gateway setting. Saving a credential does not change provider permissions, limits, '
                'pricing, or start the gateway.',
 'AGNES_KEY_6': 'Existing gateway setting. Saving a credential does not change provider permissions, limits, '
                'pricing, or start the gateway.',
 'AGNES_KEY_7': 'Existing gateway setting. Saving a credential does not change provider permissions, limits, '
                'pricing, or start the gateway.',
 'ANTHROPIC_API_KEY': 'Anthropic (Claude Haiku)',
 'ANTHROPIC_DEFAULT_HAIKU_MODEL': '',
 'ANTHROPIC_DEFAULT_SONNET_MODEL': '',
 'APPLYPILOT_AGENT_API_KEY': '',
 'APPLYPILOT_AGENT_BASE_URL': '',
 'APPLYPILOT_AGENT_MODEL': '',
 'APPLYPILOT_ALERT_WEBHOOK': '',
 'APPLYPILOT_BROWSER_RUNTIME': 'auto | browseros | playwright',
 'APPLYPILOT_PLAYWRIGHT_PYTHON': '',
 'APPLYPILOT_CRAWLER': 'http | playwright (isolated browser SDK)',
 'APPLYPILOT_DDGS_PYTHON': '',
 'APPLYPILOT_LINKEDIN_RS_BIN': '',
 'APPLYPILOT_LINKEDIN_SCRAPER_PYTHON': '',
 'APPLYPILOT_LINKEDIN_SESSION': '',
 'APPLYPILOT_MCP_CONFIG': '',
 'APPLYPILOT_RESEARCH_COMPANIES': 'Optional override: -1 uncapped (default), 0 paused, positive company cap; each <=40 connector calls, <=1 model call',
 'APPLYPILOT_RESEARCH_ON_ENRICH': '',
 'APPLYPILOT_SEARCH_BACKEND': 'auto | ddgs | searxng',
 'APPLYPILOT_TWIKIT_COOKIES': '',
 'APPLYPILOT_TWIKIT_PYTHON': '',
 'AUTH_SERVER_PORT': '',
 'BROWSEROS_MCP_URL': '',
 'CHROME_PATH': '',
 'CLIENT_ID': '',
 'CLIENT_SECRET': '',
 'CLOUDFLARE_ACCOUNT_ID': 'Cloudflare account id (required with token for /ai/models/search catalog refresh)',
 'CLOUDFLARE_API_TOKEN': 'Workers AI Read/Write token — 10,000 Neurons/day/account shared free budget, reset '
                         '00:00 UTC',
 'CLOUDFLARE_KEY_1': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                     'limits, pricing, or start the gateway.',
 'CLOUDFLARE_KEY_2': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                     'limits, pricing, or start the gateway.',
 'CLOUDFLARE_KEY_3': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                     'limits, pricing, or start the gateway.',
 'CLOUDFLARE_KEY_4': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                     'limits, pricing, or start the gateway.',
 'CLOUDFLARE_KEY_5': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                     'limits, pricing, or start the gateway.',
 'CLOUDFLARE_KEY_6': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                     'limits, pricing, or start the gateway.',
 'CLOUDFLARE_KEY_7': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                     'limits, pricing, or start the gateway.',
 'CLOUDFLARE_WORKERS_AI_OPENAI_BASE': '',
 'DEEPSEEK_API_KEY': 'DeepSeek (budget fallback)',
 'FREEHIRE_API_KEY': '',
 'FREEHIRE_API_URL': '',
 'FREEHIRE_ENABLED': '',
 'FREEHIRE_TOKEN': '',
 'GEMINI_API_BASE': '',
 'GEMINI_API_KEY': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                   'limits, pricing, or start the gateway.',
 'GEMINI_KEY_1': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                 'limits, pricing, or start the gateway.',
 'GEMINI_KEY_2': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                 'limits, pricing, or start the gateway.',
 'GEMINI_KEY_3': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                 'limits, pricing, or start the gateway.',
 'GEMINI_KEY_4': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                 'limits, pricing, or start the gateway.',
 'GEMINI_KEY_5': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                 'limits, pricing, or start the gateway.',
 'GEMINI_KEY_6': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                 'limits, pricing, or start the gateway.',
 'GEMINI_KEY_7': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                 'limits, pricing, or start the gateway.',
 'GH_TOKEN': '',
 'GITHUB_TOKEN': '',
 'GMAIL_CREDENTIALS_PATH': '',
 'GMAIL_OAUTH_PATH': '',
 'GROQ_API_KEY': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                 'limits, pricing, or start the gateway.',
 'GROQ_KEY_1': 'Existing gateway setting. Saving a credential does not change provider permissions, limits, '
               'pricing, or start the gateway.',
 'GROQ_KEY_2': 'Existing gateway setting. Saving a credential does not change provider permissions, limits, '
               'pricing, or start the gateway.',
 'GROQ_KEY_3': 'Existing gateway setting. Saving a credential does not change provider permissions, limits, '
               'pricing, or start the gateway.',
 'GROQ_KEY_4': 'Existing gateway setting. Saving a credential does not change provider permissions, limits, '
               'pricing, or start the gateway.',
 'GROQ_KEY_5': 'Existing gateway setting. Saving a credential does not change provider permissions, limits, '
               'pricing, or start the gateway.',
 'GROQ_KEY_6': 'Existing gateway setting. Saving a credential does not change provider permissions, limits, '
               'pricing, or start the gateway.',
 'GROQ_KEY_7': 'Existing gateway setting. Saving a credential does not change provider permissions, limits, '
               'pricing, or start the gateway.',
 'KILO_API_KEY': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                 'limits, pricing, or start the gateway.',
 'KILO_KEY_1': 'Existing gateway setting. Saving a credential does not change provider permissions, limits, '
               'pricing, or start the gateway.',
 'KILO_KEY_2': 'Existing gateway setting. Saving a credential does not change provider permissions, limits, '
               'pricing, or start the gateway.',
 'KILO_KEY_3': 'Existing gateway setting. Saving a credential does not change provider permissions, limits, '
               'pricing, or start the gateway.',
 'KILO_KEY_4': 'Existing gateway setting. Saving a credential does not change provider permissions, limits, '
               'pricing, or start the gateway.',
 'KILO_KEY_5': 'Existing gateway setting. Saving a credential does not change provider permissions, limits, '
               'pricing, or start the gateway.',
 'KILO_KEY_6': 'Existing gateway setting. Saving a credential does not change provider permissions, limits, '
               'pricing, or start the gateway.',
 'KILO_KEY_7': 'Existing gateway setting. Saving a credential does not change provider permissions, limits, '
               'pricing, or start the gateway.',
 'LITELLM_MASTER_KEY': '',
 'LLM_API_KEY': '',
 'LLM_MODEL': '',
 'LLM_MODEL_QUALITY': '',
 'LLM_URL': '',
 'MCP_CONFIG_DIR': '',
 'MISTRAL_ADMIN_API_KEY': 'Existing gateway setting. Saving a credential does not change provider '
                          'permissions, limits, pricing, or start the gateway.',
 'MISTRAL_API_KEY': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                    'limits, pricing, or start the gateway.',
 'MISTRAL_KEY_1': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                  'limits, pricing, or start the gateway.',
 'MISTRAL_KEY_2': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                  'limits, pricing, or start the gateway.',
 'MISTRAL_KEY_3': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                  'limits, pricing, or start the gateway.',
 'MISTRAL_KEY_4': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                  'limits, pricing, or start the gateway.',
 'MISTRAL_KEY_5': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                  'limits, pricing, or start the gateway.',
 'MISTRAL_KEY_6': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                  'limits, pricing, or start the gateway.',
 'MISTRAL_KEY_7': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                  'limits, pricing, or start the gateway.',
 'NVIDIA_NIM_API_KEY': 'NVIDIA Build / hosted NIM (free hosted endpoints)',
 'OPENAI_API_KEY': 'OpenAI (GPT-4.1 Nano)',
 'OPENCODE_ZEN_API_BASE': '',
 'OPENCODE_ZEN_API_KEY': 'Existing gateway setting. Saving a credential does not change provider '
                         'permissions, limits, pricing, or start the gateway.',
 'OPENROUTER_API_KEY': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                       'limits, pricing, or start the gateway.',
 'OPENROUTER_KEY_1': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                     'limits, pricing, or start the gateway.',
 'OPENROUTER_KEY_2': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                     'limits, pricing, or start the gateway.',
 'OPENROUTER_KEY_3': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                     'limits, pricing, or start the gateway.',
 'OPENROUTER_KEY_4': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                     'limits, pricing, or start the gateway.',
 'OPENROUTER_KEY_5': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                     'limits, pricing, or start the gateway.',
 'OPENROUTER_KEY_6': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                     'limits, pricing, or start the gateway.',
 'OPENROUTER_KEY_7': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                     'limits, pricing, or start the gateway.',
 'OPENROUTER_MANAGEMENT_API_KEY': 'Existing gateway setting. Saving a credential does not change provider '
                                  'permissions, limits, pricing, or start the gateway.',
 'OPENXLAB_INTERN_API_KEY': 'Existing gateway setting. Saving a credential does not change provider '
                            'permissions, limits, pricing, or start the gateway.',
 'OPENXLAB_INTERN_KEY_1': 'Existing gateway setting. Saving a credential does not change provider '
                          'permissions, limits, pricing, or start the gateway.',
 'ORCAROUTER_API_KEY': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                       'limits, pricing, or start the gateway.',
 'ORCAROUTER_KEY_1': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                     'limits, pricing, or start the gateway.',
 'ORCAROUTER_KEY_2': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                     'limits, pricing, or start the gateway.',
 'ORCAROUTER_KEY_3': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                     'limits, pricing, or start the gateway.',
 'ORCAROUTER_KEY_4': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                     'limits, pricing, or start the gateway.',
 'ORCAROUTER_KEY_5': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                     'limits, pricing, or start the gateway.',
 'ORCAROUTER_KEY_6': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                     'limits, pricing, or start the gateway.',
 'ORCAROUTER_KEY_7': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                     'limits, pricing, or start the gateway.',
 'POOLSIDE_API_KEY': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                     'limits, pricing, or start the gateway.',
 'POOLSIDE_KEY_1': '7 Poolside keys for lanes (POOLSIDE_KEY_1..7) — each verified individually; M.1 free '
                   'status is NOT auto-carried forward (May-2026 promo ended on the models page)',
 'PROXY': '',
 'REFRESH_TOKEN': '',
 'REQUESTY_API_KEY': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                     'limits, pricing, or start the gateway.',
 'REQUESTY_KEY_1': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                   'limits, pricing, or start the gateway.',
 'REQUESTY_KEY_2': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                   'limits, pricing, or start the gateway.',
 'REQUESTY_KEY_3': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                   'limits, pricing, or start the gateway.',
 'REQUESTY_KEY_4': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                   'limits, pricing, or start the gateway.',
 'REQUESTY_KEY_5': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                   'limits, pricing, or start the gateway.',
 'REQUESTY_KEY_6': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                   'limits, pricing, or start the gateway.',
 'REQUESTY_KEY_7': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                   'limits, pricing, or start the gateway.',
 'SEARXNG_URL': '',
 'SENSENOVA_API_KEY': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                      'limits, pricing, or start the gateway.',
 'SENSENOVA_KEY_1': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                    'limits, pricing, or start the gateway.',
 'SENSENOVA_KEY_2': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                    'limits, pricing, or start the gateway.',
 'SENSENOVA_KEY_3': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                    'limits, pricing, or start the gateway.',
 'SENSENOVA_KEY_4': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                    'limits, pricing, or start the gateway.',
 'SENSENOVA_KEY_5': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                    'limits, pricing, or start the gateway.',
 'SENSENOVA_KEY_6': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                    'limits, pricing, or start the gateway.',
 'SENSENOVA_KEY_7': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                    'limits, pricing, or start the gateway.',
 'SILICONFLOW_API_KEY': 'SiliconFlow free models cost zero; rate limits account-level, fixed per model, '
                        'independent (not a shared pool)',
 'SILICONFLOW_KEY_1': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                      'limits, pricing, or start the gateway.',
 'SILICONFLOW_KEY_2': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                      'limits, pricing, or start the gateway.',
 'SILICONFLOW_KEY_3': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                      'limits, pricing, or start the gateway.',
 'SILICONFLOW_KEY_4': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                      'limits, pricing, or start the gateway.',
 'SILICONFLOW_KEY_5': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                      'limits, pricing, or start the gateway.',
 'SILICONFLOW_KEY_6': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                      'limits, pricing, or start the gateway.',
 'SILICONFLOW_KEY_7': 'Existing gateway setting. Saving a credential does not change provider permissions, '
                      'limits, pricing, or start the gateway.',
 'XACTIONS_SESSION_COOKIE': 'local XActions only; your own authorized session',
 'ZAI_API_KEY': 'Existing gateway setting. Saving a credential does not change provider permissions, limits, '
                'pricing, or start the gateway.',
 'ZAI_KEY_1': 'Existing gateway setting. Saving a credential does not change provider permissions, limits, '
              'pricing, or start the gateway.',
 'ZAI_KEY_2': 'Existing gateway setting. Saving a credential does not change provider permissions, limits, '
              'pricing, or start the gateway.',
 'ZAI_KEY_3': 'Existing gateway setting. Saving a credential does not change provider permissions, limits, '
              'pricing, or start the gateway.',
 'ZAI_KEY_4': 'Existing gateway setting. Saving a credential does not change provider permissions, limits, '
              'pricing, or start the gateway.',
 'ZAI_KEY_5': 'Existing gateway setting. Saving a credential does not change provider permissions, limits, '
              'pricing, or start the gateway.',
 'ZAI_KEY_6': 'Existing gateway setting. Saving a credential does not change provider permissions, limits, '
              'pricing, or start the gateway.',
 'ZAI_KEY_7': 'Existing gateway setting. Saving a credential does not change provider permissions, limits, '
              'pricing, or start the gateway.',
 'ZEN_KEY_1': 'Existing gateway setting. Saving a credential does not change provider permissions, limits, '
              'pricing, or start the gateway.',
 'ZEN_KEY_2': 'Existing gateway setting. Saving a credential does not change provider permissions, limits, '
              'pricing, or start the gateway.',
 'ZEN_KEY_3': 'Existing gateway setting. Saving a credential does not change provider permissions, limits, '
              'pricing, or start the gateway.',
 'ZEN_KEY_4': 'Existing gateway setting. Saving a credential does not change provider permissions, limits, '
              'pricing, or start the gateway.',
 'ZEN_KEY_5': 'Existing gateway setting. Saving a credential does not change provider permissions, limits, '
              'pricing, or start the gateway.',
 'ZEN_KEY_6': 'Existing gateway setting. Saving a credential does not change provider permissions, limits, '
              'pricing, or start the gateway.',
 'ZEN_KEY_7': 'Existing gateway setting. Saving a credential does not change provider permissions, limits, '
              'pricing, or start the gateway.'}

ENV_HINTS.update({
    "APPLYPILOT_BACKUP_TOKEN": "Fine-grained GitHub token: selected PRIVATE recovery repository, Contents read/write. Separate from the read-only evidence token.",
})

# Managed local runtimes; no new paid providers or arbitrary browser commands.
ENV_HINTS.update({
 'APPLYPILOT_SOURCE_RUNTIME':'auto preserves an existing local SDK; otherwise uses the source-tools Docker container. local or docker can be selected explicitly.',
 'APPLYPILOT_GMAIL_RUNTIME':'auto prefers the installed Gmail MCP in source-tools; local uses npx. Both use your existing credentials.',
 'APPLYPILOT_CRAWLER':'auto uses HTTP first and renders thin pages with Playwright; hybrid combines both for every permitted page.',
 'APPLYPILOT_LINKEDIN_RS_SESSION':'Existing linkedin-rs session.json. Leave empty for unambiguous standard-location detection.',
 'FREEHIRE_CREDENTIALS_PATH':'Optional explicit CLI creds.json; otherwise the existing ~/.freehire/creds.json is detected.',
})

ENV_HINTS.update({
    "APPLYPILOT_OUTREACH_ENABLED": "Outreach uses your saved accounts by default. Set false to disable networking while discovery and applications continue.",
    "APPLYPILOT_RLM_ENABLED": "Enable recursive analysis of browser observations and long-context workflows (default true). Observation archiving remains active.",
    "APPLYPILOT_RLM_THRESHOLD_CHARS": "Context length that enables recursive analysis (default 20000 characters).",
    "APPLYPILOT_RLM_MAX_CALLS": "Maximum gateway calls for a standalone RLM invocation (default 32).",
    "APPLYPILOT_RLM_MAX_ITERATIONS": "Maximum recursive analysis iterations (default 24).",
    "APPLYPILOT_RLM_TIMEOUT_SECONDS": "Optional RLM wall-clock timeout; 0 leaves it disabled.",
    "APPLYPILOT_RLM_PARALLEL_CALLS": "Concurrent recursive subcalls (default 2).",
    "APPLYPILOT_RLM_DOCKER_IMAGE": "Local Python image for network-isolated recursive analysis (default python:3.11-slim). Pull the image before starting workers.",
    "APPLYPILOT_DSPY_ENABLED": "Use DSPy task prompts and compatible learned revisions (default true).",
    "APPLYPILOT_LEARNING_MODE": "full (default) optimizes and promotes passing candidates; optimize requires manual promotion; capture only records evidence.",
    "APPLYPILOT_LEARNING_ENABLED": "Optional override of learning mode; saved learning choices still win.",
    "APPLYPILOT_LEARNING_AUTO_PROMOTE": "Automatically deploy candidates that pass held-out evaluation.",
    "APPLYPILOT_LEARNING_ALLOW_JUDGED_PROMOTION": "Accept model-judged/next-action-proxy evidence, which can be wrong.",
    "APPLYPILOT_LEARNING_CAPTURE": "Capture private local evidence; no model calls from capture itself.",
    "APPLYPILOT_WEB_PORT": "Preferred dashboard loopback port (8765); adjacent ports are used when busy.",
    "APPLYPILOT_OPEN_DASHBOARD": "Open the dashboard on applypilot start (default true).",
    "APPLYPILOT_START_DASHBOARD_WAIT_SECONDS": "Bounded CLI wait for a verified dashboard URL (0–120).",
    "APPLYPILOT_BACKUP_ENABLED": "Explicit true authorizes automatic private GitHub uploads once GitHub access is configured. Saved backup choices win.",
    "APPLYPILOT_BACKUP_REPOSITORY": "Private backup repository in owner/name format.",
    "APPLYPILOT_BACKUP_STATIC_MINUTES": "Small Git/policy checkpoint interval (1–1440 minutes).",
    "APPLYPILOT_BACKUP_SNAPSHOT_HOURS": "Full encrypted snapshot interval (1–168 hours).",
    "APPLYPILOT_BACKUP_INCLUDE_DOCUMENTS": "Include uploaded documents in full encrypted snapshots (default true).",
})

ENV_HINTS.update({
    "APPLYPILOT_DATABASE_URL": "Optional dedicated PostgreSQL URI; contains credentials. Local-file managed.",
    "APPLYPILOT_PGDATA": "Private pgembed cluster directory; local-file managed. Existing clusters retain their provider marker.",
    "APPLYPILOT_DB_POOL_SIZE": "Automatic PostgreSQL pool sizing (blank or auto): 2 × shared workers + 8 connections per process. A positive integer sets a minimum that automatic sizing raises when needed. Worker startup resizes existing pools.",
    "APPLYPILOT_RESOURCE_WORKERS": "Shared execution workers for preparation, applications and supporting services; restart to apply.",
    "APPLYPILOT_BROWSER_WORKERS": "Simultaneous browser tasks; defaults to at most two and must not exceed the worker total.",
    "APPLYPILOT_PREPARATION_MAX_PENDING": "Maximum queued and active preparation tasks; must be a positive integer.",
    "APPLYPILOT_SALARY_ENRICHMENT": "Optional best-effort salary enrichment after scoring commits; default false.",
    "APPLYPILOT_SEC_USER_AGENT": "SEC EDGAR contact User-Agent: your application name and contact email. Required for Form D feeds.",
    "DEALROOM_API_KEY": "Licensed Dealroom Premium v1 key. Public sample feeds do not require this key.",
    "CRUNCHBASE_API_KEY": "Licensed Crunchbase v4 API key for company and funding searches.",
    "MAGNITT_API_KEY": "Licensed MAGNiTT key for an account-provided company-feed-contracts.json endpoint contract.",
})
