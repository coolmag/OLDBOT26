import asyncio
import logging
import random
from pathlib import Path
from typing import List, Optional, Tuple

import httpx
from v2ray2proxy import V2RayProxy

logger = logging.getLogger("proxy_daemon")

class ProxyManager:
    def __init__(self, proxy_list_path: Path):
        self._proxy_list_path = proxy_list_path
        self._proxies: List[str] = []
        self._active_proxy: Optional[V2RayProxy] = None
        self._active_proxy_url: Optional[str] = None
        self._is_running = False
        self._watchdog_task: Optional[asyncio.Task] = None
        self._load_proxies()

    def _load_proxies(self):
        if not self._proxy_list_path.exists():
            return
        with open(self._proxy_list_path, 'r', encoding='utf-8') as f:
            self._proxies = [line.strip() for line in f if line.strip() and "security=reality" not in line.lower()]
        random.shuffle(self._proxies)

    async def start_daemon(self):
        """Запускает фоновый процесс поддержания прокси"""
        if self._is_running: return
        self._is_running = True
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        logger.info("🛡 Proxy Watchdog daemon started.")

    async def stop_daemon(self):
        self._is_running = False
        if self._watchdog_task: self._watchdog_task.cancel()
        self._stop_current_proxy()

    def _stop_current_proxy(self):
        if self._active_proxy:
            self._active_proxy.stop()
            self._active_proxy = None
            self._active_proxy_url = None

    async def _watchdog_loop(self):
        while self._is_running:
            if not self._active_proxy_url or not await self._test_proxy_connection(self._active_proxy_url)[0]:
                logger.warning("🔄 Proxy is down or not set. Rotating...")
                await self._rotate_proxy()
            await asyncio.sleep(60) # Проверяем здоровье раз в минуту

    async def _rotate_proxy(self):
        self._stop_current_proxy()
        if not self._proxies:
            self._load_proxies()
        
        for proxy_link in self._proxies:
            if not self._is_running: break
            try:
                temp_proxy = V2RayProxy(proxy_link)
                await asyncio.sleep(3) # Ждем поднятия туннеля
                if getattr(temp_proxy, 'socks_proxy_url', None):
                    url = temp_proxy.socks_proxy_url
                    is_ok, error_reason = await self._test_proxy_connection(url)
                    if is_ok:
                        self._active_proxy = temp_proxy
                        self._active_proxy_url = url
                        logger.info(f"✅ V2Ray Proxy UP: {url}")
                        return
                    else:
                        logger.warning(f"Proxy {url} failed health check: {error_reason}")
                temp_proxy.stop()
            except Exception as e:
                logger.error(f"Failed to start proxy node: {e}")
        logger.error("❌ All proxies exhausted.")

    async def _test_proxy_connection(self, proxy_url: str) -> Tuple[bool, Optional[str]]:
        try:
            async with httpx.AsyncClient(proxies={"all://": proxy_url}, timeout=10.0, verify=False) as client:
                resp = await client.get("http://cp.cloudflare.com/generate_204")
                return resp.status_code in [200, 204], None
        except Exception as e:
            return False, str(e)

    @property
    def active_proxy_url(self) -> Optional[str]:
        return self._active_proxy_url
