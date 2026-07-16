# backend/system_tools/weather.py
import asyncio
from typing import Any, Dict


async def get_weather(location: str, days: int = 1) -> Dict[str, Any]:
    """
    获取指定城市的天气信息。

    所有同步 I/O 放入线程池执行，防止阻塞事件循环导致
    delegate_task 等嵌套调用永久卡死。
    """
    import requests as _requests

    loop = asyncio.get_running_loop()

    def _sync_fetch():
        try:
            res = _requests.get(
                f"https://wttr.in/{location}?T&{days}",
                timeout=10,  # 必须设置超时，否则线程池线程也会永久阻塞
                headers={"User-Agent": "MyWorkbench/1.0"},
            )
            if res.status_code == 200:
                return {"success": True, "content": res.text}
            else:
                return {"success": False, "content": f"查询天气失败，HTTP {res.status_code}"}
        except _requests.RequestException as e:
            return {"success": False, "content": f"天气服务请求失败：{str(e)}"}

    return await loop.run_in_executor(None, _sync_fetch)
