"""Explicit, independently checkpointed hiring and referral searches."""
import re


def hiring_queries(role):
    role = " ".join(role.split())
    phrases = ("hiring", "#hiring", '"we\'re hiring"', '"join our team"',
               '"happy to refer"', '"send your resume"')
    queries = [f"{role} {phrase}" for phrase in phrases]
    hashtag = re.sub(r"[^\w]", "", role)
    if hashtag:
        queries.append(f"#{hashtag} #hiring")
    return tuple(dict.fromkeys(queries))
