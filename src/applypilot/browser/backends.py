"""Private browser adapter scripts. Cookie values never become model tool output."""
from __future__ import annotations

import json

from applypilot.browser.clearance import ClearanceSolution


class BrowserOSBackend:
    name = "browseros"

    def capture_script(self, page_id: int) -> str:
        return f"const page={json.dumps(page_id)};" + r"""
const cdp=(method,params)=>browser.cdpJsonForPage(page,method,JSON.stringify(params));
const result=await cdp('Runtime.evaluate',{expression:'location.href',returnByValue:true});
if(result.exceptionDetails||!result.result?.value)throw new Error('Cannot read the owned page URL');
const url=result.result.value;
const {cookies}=await cdp('Network.getCookies',{urls:[url]});
return {url,cookies};
"""

    def apply_clearance_script(self, page_id: int, solution: ClearanceSolution, *, expected_url: str) -> str:
        payload = {"page": page_id, "url": expected_url,
                   "cookies": [cookie.to_browser_os_dict() for cookie in solution.cookies],
                   "userAgent": solution.user_agent}
        return "const input=" + json.dumps(payload) + ";" + r"""
const cdp=(method,params)=>browser.cdpJsonForPage(input.page,method,JSON.stringify(params));
const current=await cdp('Runtime.evaluate',{expression:'location.href',returnByValue:true});
if(current.exceptionDetails||current.result?.value!==input.url)throw new Error('Owned page navigated during challenge recovery');
await cdp('Network.setCookies',{cookies:input.cookies});
await cdp('Emulation.setUserAgentOverride',{userAgent:input.userAgent});
await browser.nav(input.page).reload();
return {applied:true};
"""

    def observation_script(self, page_id: int) -> str:
        expression = "({url:location.href,title:document.title,text:(document.body?.innerText||'').slice(0,12000)})"
        return (
            "const result=await browser.cdpJsonForPage(" + json.dumps(page_id)
            + ",'Runtime.evaluate',JSON.stringify({expression:" + json.dumps(expression)
            + ",returnByValue:true}));"
            "if(result.exceptionDetails||!result.result?.value)throw new Error('Cannot observe the recovered page');"
            "return result.result.value;"
        )
