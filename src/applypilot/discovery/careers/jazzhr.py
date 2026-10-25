"""JazzHR public career HTML. No invented public/private jobs API access."""
from applypilot.discovery.careers.base import CareerParser
from applypilot.discovery.careers.generic import execute_html_task


def execute_task(task, context, cursor=None):
    return execute_html_task(task, context, cursor, parser="jazzhr")


class JazzHRCareerParser(CareerParser):
    provider = "jazzhr"
