import json
import logging
import os
import re
import time
import base64
from bs4 import BeautifulSoup
from curl_cffi import requests

# 为了加密 GitHub Secret
try:
    from nacl import encoding, public
    HAS_NACL = True
except ImportError:
    HAS_NACL = False

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class HidenCloud:
    def __init__(self, cookie_str, tg_config=None, proxy=None):
        self.base_url = "https://dash.hidencloud.com"
        self.cookie_str = cookie_str
        self.tg_config = tg_config
        
        # 1. 规范化代理协议头（自动转换 socks5 为 socks5h，防止 DNS 污染）
        if proxy:
            proxy = proxy.strip()
            if proxy.startswith("socks5://"):
                proxy = proxy.replace("socks5://", "socks5h://")
            elif not proxy.startswith("http://") and not proxy.startswith("https://") and not proxy.startswith("socks"):
                # 如果用户漏写了协议头，默认补上 socks5h://
                proxy = f"socks5h://{proxy}"
                
        self.proxy = proxy
        
        # 2. 终极多重代理绑定（通杀所有版本的 curl_cffi）
        self.session = requests.Session(impersonate="chrome110")
        if self.proxy:
            self.session.proxy = self.proxy  # 针对新版 curl_cffi 属性
            self.session.proxies = {         # 针对传统兼容层
                "http": self.proxy,
                "https": self.proxy
            }
            
        self.username = "Unknown"
        self.balance = "未知"
        self.updated_cookies = False
        self.csrf_token = ""
        self.parse_and_set_cookies()
        self.test_proxy_ip()  # 初始化时测试代理状态

    def test_proxy_ip(self):
        """测试代理连通性并输出当前出口 IP"""
        if not self.proxy:
            logger.info("ℹ️ 未配置代理，将使用 GitHub Actions 默认 IP 直连")
            return
        try:
            # 使用 session 发送请求，检测代理是否生效
            resp = self.session.get("https://httpbin.org/ip", timeout=10)
            if resp.status_code == 200:
                current_ip = resp.json().get("origin")
                logger.info(f"🌐 代理连接成功！当前网络出口 IP: {current_ip}")
            else:
                logger.warning(f"⚠️ 代理连接返回异常状态码: {resp.status_code}，尝试继续任务...")
        except Exception as e:
            logger.error(f"❌ 代理连通性测试失败，请检查代理节点是否存活: {e}")

    def parse_and_set_cookies(self):
        """解析 Cookie 字符串并设置到 Session"""
        if not self.cookie_str:
            return
        
        cookies = {}
        for item in self.cookie_str.split(';'):
            if '=' in item:
                parts = item.strip().split('=', 1)
                if len(parts) == 2:
                    key, value = parts
                    cookies[key] = value
        self.session.cookies.update(cookies)

    def get_cookie_string(self):
        """获取当前的 Cookie 字符串 (针对 curl_cffi 底层 cookiejar 的终极提取优化)"""
        cookie_dict = {}
        
        # 1. 首先尝试从底层标准 cookie 罐子里提取 (解决从代理握手中漏掉的 Cookie)
        try:
            if hasattr(self.session.cookies, "jar"):
                for cookie in self.session.cookies.jar:
                    cookie_dict[cookie.name] = cookie.value
        except Exception as e:
            logger.debug(f"从 jar 提取 cookie 失败: {e}")

        # 2. 其次合并当前 session 自带的 items() 字典内容
        try:
            for name, value in self.session.cookies.items():
                cookie_dict[name] = value
        except Exception:
            pass

        # 3. 如果通过上面两种机制都没拿到，最后保底使用最初传入的原始 Cookie，防止变为空值
        if not cookie_dict and self.cookie_str:
            return self.cookie_str

        # 将去重后的全量字典组装回标准 cookie 字符串
        cookie_list = [f"{name}={value}" for name, value in cookie_dict.items()]
        return "; ".join(cookie_list)

    def update_github_secret(self, new_cookie):
        """自动更新 GitHub Secret"""
        gh_pat = os.environ.get("GH_PAT")
        repo = os.environ.get("GITHUB_REPOSITORY")
        secret_name = "HIDEN_COOKIE"

        if not gh_pat or not repo:
            logger.warning("未找到 GH_PAT 或 GITHUB_REPOSITORY，跳过 Secret 更新")
            return

        if not HAS_NACL:
            logger.error("未安装 pynacl 库，无法加密并更新 Secret")
            return

        logger.info(f"正在尝试更新 GitHub Secret: {secret_name}")
        headers = {
            "Authorization": f"token {gh_pat}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "HidenCloud-Renew-Bot"
        }

        try:
            # 1. 获取公钥
            pub_key_resp = requests.get(
                f"https://api.github.com/repos/{repo}/actions/secrets/public-key",
                headers=headers
            )
            if pub_key_resp.status_code != 200:
                logger.error(f"获取公钥失败: {pub_key_resp.text}")
                return
            
            pub_key_data = pub_key_resp.json()
            public_key = pub_key_data['key']
            key_id = pub_key_data['key_id']

            # 2. 加密 Secret
            def encrypt(public_key: str, secret_value: str) -> str:
                public_key = public.PublicKey(public_key.encode("utf-8"), encoding.Base64Encoder())
                sealed_box = public.SealedBox(public_key)
                encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
                return base64.b64encode(encrypted).decode("utf-8")

            encrypted_value = encrypt(public_key, new_cookie)

            # 3. 提交更新
            put_resp = requests.put(
                f"https://api.github.com/repos/{repo}/actions/secrets/{secret_name}",
                headers=headers,
                json={
                    "encrypted_value": encrypted_value,
                    "key_id": key_id
                }
            )
            if put_resp.status_code in [201, 204]:
                logger.info(f"✅ GitHub Secret {secret_name} 更新成功！")
            else:
                logger.error(f"❌ 更新 Secret 失败: {put_resp.text}")

        except Exception as e:
            logger.error(f"更新 Secret 过程出错: {e}")

    def get_hitokoto(self):
        """获取每日一言（使用独立会话与独立代理传参）"""
        try:
            hitokoto_session = requests.Session(impersonate="chrome110")
            if self.proxy:
                hitokoto_session.proxy = self.proxy
                hitokoto_session.proxies = {"http": self.proxy, "https": self.proxy}
                
            resp = hitokoto_session.get("https://v1.hitokoto.cn/?encode=json", timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                return f"『{data['hitokoto']}』—— {data['from']}"
        except Exception:
            pass
        return "保持热爱，奔赴山海。"

    def send_tg_notification(self, message):
        """发送 Telegram 通知（使用独立会话，防止 Cookie 污染与冲突报错）"""
        if not self.tg_config or not self.tg_config.get("bot_token") or not self.tg_config.get("chat_id"):
            return

        url = f"https://api.telegram.org/bot{self.tg_config['bot_token']}/sendMessage"
        
        hitokoto = self.get_hitokoto()
        formatted_message = (
            f"☁️ **HidenCloud 自动续费任务**\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"👤 **账号**: `{self.username}`\n"
            f"💰 **余额**: `{self.balance}`\n"
            f"🕒 **时间**: `{time.strftime('%Y-%m-%d %H:%M:%S')}`\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{message}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💡 **每日一言**:\n_{hitokoto}_"
        )

        payload = {
            "chat_id": self.tg_config["chat_id"],
            "text": formatted_message,
            "parse_mode": "Markdown"
        }
        try:
            # 独立创建一个临时的 TG 网络会话，配置单独的独立代理参数
            tg_session = requests.Session(impersonate="chrome110")
            if self.proxy:
                tg_session.proxy = self.proxy
                tg_session.proxies = {"http": self.proxy, "https": self.proxy}
                
            resp = tg_session.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                logger.info("Telegram 通知发送成功")
            else:
                logger.error(f"Telegram 通知发送失败: {resp.text}")
        except Exception as e:
            logger.error(f"发送 Telegram 通知出错: {e}")

    def get_csrf_token(self, url=None, html=None):
        """从页面或 HTML 中提取 CSRF Token"""
        try:
            if html is None and url:
                resp = self.session.get(url, timeout=20)
                html = resp.text
            
            if not html:
                return None
                
            soup = BeautifulSoup(html, 'html.parser')
            token_meta = soup.find('meta', attrs={'name': 'csrf-token'})
            if token_meta:
                self.csrf_token = token_meta.get('content')
                return self.csrf_token
            
            token_input = soup.find('input', attrs={'name': '_token'})
            if token_input:
                self.csrf_token = token_input.get('value')
                return self.csrf_token
        except Exception as e:
            logger.error(f"获取 CSRF Token 失败: {e}")
        return self.csrf_token

    def check_login(self):
        """检查登录状态并获取用户名"""
        try:
            resp = self.session.get(f"{self.base_url}/dashboard", timeout=20, allow_redirects=True)
            if "/login" in resp.url:
                return False
                
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, 'html.parser')
                self.get_csrf_token(html=resp.text)
                
                email_tag = soup.select_one('p.font-light.text-gray-500')
                if not email_tag:
                    email_tag = soup.find('p', string=re.compile(r'.+@.+\..+'))
                
                if email_tag and "[email" not in email_tag.get_text():
                    self.username = email_tag.get_text().strip()
                else:
                    name_tag = soup.select_one('h3 > a[href="#"]')
                    if name_tag:
                        self.username = name_tag.get_text().strip()
                    elif email_tag:
                        self.username = email_tag.get_text().strip()

                balance_link = soup.select_one('a[href*="/balance"]')
                if balance_link:
                    balance_tag = balance_link.find(['dt', 'h4', 'div'], class_=re.compile(r'font-extrabold|text-3xl'))
                    if balance_tag:
                        self.balance = balance_tag.get_text().strip()
                
                if self.balance == "未知":
                    balance_text = soup.find(string=re.compile(r'(¥|€|余额)\s*\d+\.\d+'))
                    if balance_text:
                        self.balance = balance_text.strip()
                
                logger.info(f"✅ 账号 {self.username} 登录成功 (余额: {self.balance})")
                return True
        except Exception as e:
            logger.error(f"登录状态检查异常: {e}")
        return False

    def get_service_ids(self):
        """获取所有服务 ID"""
        logger.info("正在获取服务列表...")
        try:
            resp = self.session.get(f"{self.base_url}/dashboard", timeout=20)
            ids = re.findall(r'service/(\d+)/manage', resp.text)
            return list(set(ids))
        except Exception as e:
            logger.error(f"获取服务 ID 失败: {e}")
            return []

    def renew_service(self, service_id):
        """对指定服务进行续期"""
        logger.info(f"正在为服务 {service_id} 申请续期...")
        manage_url = f"{self.base_url}/service/{service_id}/manage"
        
        resp = self.session.get(manage_url, timeout=20)
        token = self.get_csrf_token(html=resp.text)
        if not token:
            return False, "获取续期 Token 失败"

        renew_url = f"{self.base_url}/service/{service_id}/renew"
        data = {
            "_token": token,
            "days": "7"
        }
        
        # 兼容处理多域名情况，直接指定域名获取 XSRF-TOKEN
        xsrf_token = self.session.cookies.get("XSRF-TOKEN", domain="dash.hidencloud.com")
        headers = {
            "Referer": manage_url,
            "Origin": self.base_url,
            "X-CSRF-TOKEN": token
        }
        if xsrf_token:
