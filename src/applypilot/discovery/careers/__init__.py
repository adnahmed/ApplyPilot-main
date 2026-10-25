"""Canonical provider detection and explicit internal-parser dispatch."""
from urllib.parse import urlsplit

from applypilot.discovery.careers.base import CareerJob, CareerParser

_DOMAINS = {
    "greenhouse": ("greenhouse.io",), "lever": ("lever.co", "lever.eu"),
    "ashby": ("ashbyhq.com",), "workable": ("workable.com",),
    "smartrecruiters": ("smartrecruiters.com",), "bamboohr": ("bamboohr.com",),
    "jazzhr": ("jazz.co", "jazzhr.com", "applytojob.com", "theresumator.com"),
    "personio": ("jobs.personio.de", "jobs.personio.com"),
    "recruitee": ("recruitee.com",), "teamtailor": ("teamtailor.com",),
    "workday": ("myworkdayjobs.com",), "icims": ("icims.com",), "jobvite": ("jobvite.com",),
}


def detect_provider(career_url):
    host = (urlsplit(career_url).hostname or "").lower()
    return next((provider for provider, domains in _DOMAINS.items()
                 if any(host == domain or host.endswith("." + domain) for domain in domains)), "generic")


def parser_module(provider):
    from importlib import import_module
    if provider not in {"workable", "smartrecruiters", "bamboohr", "jazzhr", "generic"}:
        raise ValueError("Unsupported internal career parser: " + str(provider))
    return import_module("applypilot.discovery.careers." + provider)


def get_parser(career_url, company_name="", *, context=None, tenant=None):
    provider = detect_provider(career_url)
    if provider in {"greenhouse", "lever", "ashby"}:
        parser = CareerParser(career_url, company_name, context=context, tenant=tenant)
        parser.provider = provider
        return parser
    if provider not in {"workable", "smartrecruiters", "bamboohr", "jazzhr"}:
        provider = "generic"
    module = parser_module(provider)
    classes = {"workable": "WorkableCareerParser", "smartrecruiters": "SmartRecruitersCareerParser",
               "bamboohr": "BambooHRCareerParser", "jazzhr": "JazzHRCareerParser", "generic": "GenericCareerParser"}
    return getattr(module, classes[provider])(career_url, company_name, context=context, tenant=tenant)


__all__ = ["CareerJob", "CareerParser", "detect_provider", "get_parser", "parser_module"]
