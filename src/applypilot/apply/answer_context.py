"""Shared facts, search intent and completion policy for application answers."""

from __future__ import annotations

from dataclasses import asdict
import re

from applypilot import config
from applypilot.discovery.countries import COUNTRY_NAMES
from applypilot.profile import (
    application_profile_projection,
    scoped_work_authorization,
    selected_arrangement_data,
)


ANSWER_POLICY = """Use explicit candidate facts first, scoped to the question's country.
The country literally named in a question remains its country: a US authorization
question is not a Pakistan authorization question. Employer headquarters, applicant
residence and the physical work country are distinct. Search preferences describe
desired arrangements, not verified legal status. Use the selected work arrangement
and active search preferences to distinguish remote work from the candidate's country,
contractor/EOR engagement, direct employment and relocation.
An unknown/null fact does not imply either Yes or No. Under the owner's optimistic
completion policy, generate one concrete contextual answer for every remaining required
field, including authorization, sponsorship and compensation. Prefer a Recommended
choice when consistent with explicit facts and the actual arrangement, otherwise choose
the best fitting listed option or a contextual answer/estimate. Missing information is
not a reason to ask for clarification, leave a placeholder or block the application.
This completion rule also applies where other guidance suggests deferring a missing
answer. Do not reuse unscoped saved Q&A for country-specific authorization or sponsorship.
Save inferred answers only as generated application answers for this job; they never
become verified facts or change the candidate profile, resume or search configuration.
"""


def is_placeholder_answer(answer: object) -> bool:
    if not isinstance(answer, str) or not answer.strip():
        return True
    text = answer.strip().casefold()
    return (text in {"unknown", "unanswered", "not provided", "not recorded", "null", "tbd"}
            or bool(re.match(r"https?://(?:www\.)?example\.(?:com|org|net)(?:[/:?#]|$)", text))
            or text.startswith("unanswered ")
            or "candidate confirmation required" in text
            or "requires candidate confirmation" in text)


def is_authorization_question(question: str) -> bool:
    return any(term in question.casefold() for term in (
        "sponsor", "authorized to work", "authorised to work", "eligible to work",
        "right to work", "work authorization", "work authorisation", "work permit",
    ))


def question_authorization_context(question: str, profile: dict, job: dict) -> dict | None:
    lowered = question.casefold()
    sponsorship = "sponsor" in lowered
    authorization = any(term in lowered for term in (
        "authorized to work", "authorised to work", "eligible to work", "right to work",
        "work authorization", "work authorisation", "work permit",
    ))
    if not is_authorization_question(question):
        return None
    countries = {code for name, code in COUNTRY_NAMES.items()
                 if re.search(rf"(?<![a-z]){re.escape(name)}(?![a-z])", lowered)}
    aliases = {"US": "US", "USA": "US", "UK": "GB", "UAE": "AE", "PK": "PK"}
    countries.update(code for name, code in aliases.items()
                     if re.search(rf"\b{name}\b", question)
                     or re.search(rf"\bin (?:the )?{name.casefold()}\b", lowered))
    if re.search(r"\bu\.s\.(?:a\.)?", lowered):
        countries.add("US")
    facts = ({country: scoped_work_authorization(profile, {**job, "work_country": country})
              for country in sorted(countries)} if countries else
             {"selected_arrangement": scoped_work_authorization(profile, job)})
    return {"authorization_question": authorization, "sponsorship_question": sponsorship,
            "explicit_countries": sorted(countries), "facts": facts}


def application_answer_context(job: dict, profile: dict) -> dict:
    from applypilot.compensation import instructions as compensation_instructions
    raw_config = config.load_search_config()
    intent, applicant = config.load_compiled_intent(raw_config, profile=profile)
    return {
        "compensation_policy": compensation_instructions(profile, job),
        "profile": application_profile_projection(profile, job),
        "selected_work_arrangement": dict(selected_arrangement_data(job) or {}),
        "candidate_residence_country": applicant.country,
        "domestic_work_authorization": scoped_work_authorization(
            profile, {"work_country": applicant.country}),
        "search_intent": {
            "fingerprint": intent.fingerprint(),
            "active_markets": [asdict(market) for market in intent.active_markets()],
            "preferences": [asdict(preference) for preference in intent.preferences],
        },
    }
