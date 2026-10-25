"""BambooHR public careers HTML, never employer-side applicant-tracking APIs."""
from applypilot.discovery.careers.base import CareerParser
from applypilot.discovery.careers.generic import execute_html_task


def execute_task(task, context, cursor=None):
    return execute_html_task(task, context, cursor, parser="bamboohr")


class BambooHRCareerParser(CareerParser):
    provider = "bamboohr"
