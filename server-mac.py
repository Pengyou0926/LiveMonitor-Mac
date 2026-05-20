import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from fastapi.responses import HTMLResponse, FileResponse
import threading
import time
import os
import re
import datetime
import json
import sys
import webbrowser
import ctypes
from PIL import Image
from pystray import Icon, Menu, MenuItem

# --- 【关键修复】使用原生 requests 库解决网络超时问题 ---
import requests 
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==========================================
# 路径兼容与文件管理
# ==========================================
def get_resource_path(relative_path):
    """ 获取打包后的内部静态资源路径 (HTML, 图标) """
    if getattr(sys, 'frozen', False):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)

def get_external_path(filename):
    """ 获取外部数据文件路径 (跨平台优化版) """
    if getattr(sys, 'frozen', False):
        # 如果是打包后的环境
        if sys.platform == "darwin":  # macOS 环境
            # 放在 Mac 用户的: 文稿/LiveMonitor_Data 目录下
            base_dir = os.path.expanduser("~/Documents/LiveMonitor_Data")
        else:  # Windows 环境
            # Windows 依然放在 EXE 同级目录
            base_dir = os.path.dirname(sys.executable)
    else:
        # 本地开发环境
        base_dir = os.path.abspath(".")
    
    # 如果目录不存在，自动帮用户建一个
    if not os.path.exists(base_dir):
        try:
            os.makedirs(base_dir)
        except Exception as e:
            pass # 忽略权限报错，避免闪退
            
    return os.path.join(base_dir, filename)

# --- 配置文件路径定义 ---
DATA_FILENAME = get_external_path("data.json")
COOKIE_FILENAME = get_external_path("cookie.txt")
OCEAN_COOKIE_FILENAME = get_external_path("ocean_cookie.txt")
OCEAN_ACCOUNTS_FILE = get_external_path("ocean_accounts.json")
TARGET_OPEN_IDS_FILE = get_external_path("target_open_ids.json")
LOG_FILENAME = get_external_path("run.log")

ADMIN_PASSWORD = "030699"  
API_REFRESH_INTERVAL = 15 

# ==========================================
# Windows 黑框 (控制台) 动态控制
# ==========================================
console_visible = True

def toggle_console(icon=None, item=None):
    """ 动态显示/隐藏控制台 (仅限 Windows) """
    if sys.platform != "win32":
        return  # macOS 不需要此功能，直接返回
        
    global console_visible
    kernel32 = ctypes.WinDLL('kernel32')
    user32 = ctypes.WinDLL('user32')
    hWnd = kernel32.GetConsoleWindow()
    if hWnd:
        if console_visible:
            user32.ShowWindow(hWnd, 0) # 0 = SW_HIDE
            console_visible = False
        else:
            user32.ShowWindow(hWnd, 5) # 5 = SW_SHOW
            console_visible = True

# ==========================================
# 系统托盘控制
# ==========================================
def open_url(url):
    webbrowser.open(url)

def quit_app(icon, item):
    icon.stop()
    os._exit(0) # 强制干净利落地结束所有线程

def setup_tray():
    """ 初始化系统托盘 """
    try:
        icon_img = Image.open(get_resource_path("icon.png"))
    except:
        # 如果没有准备 icon.png，生成一个纯色图片作为后备
        icon_img = Image.new('RGB', (64, 64), color=(56, 189, 248))
        
    menu = Menu(
        MenuItem("打开数据大屏", lambda: open_url("http://127.0.0.1:8000/"), default=True),
        MenuItem("打开后台管理", lambda: open_url("http://127.0.0.1:8000/admin")),
        MenuItem("显示/隐藏控制台日志", toggle_console),
        MenuItem("退出程序", quit_app)
    )
    
    icon = Icon("LiveMonitor", icon_img, "魏牌直播监控系统", menu)
    icon.run()

# ==========================================
# 全局数据与日志
# ==========================================
class GlobalData:
    def __init__(self):
        self.stores_data = {}  
        self.last_update = datetime.datetime.now()
        self.ocean_accounts = {}
        self.target_open_ids = []
        self.lock = threading.Lock() 
        self.log_lock = threading.Lock() 

db = GlobalData()

def logger(content):
    timestamp = datetime.datetime.now().strftime("[%H:%M:%S] ")
    msg = f"{timestamp}{content}"
    print(msg) 
    try:
        with db.log_lock: 
            with open(LOG_FILENAME, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
            with open(LOG_FILENAME, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) > 500:
                with open(LOG_FILENAME, "w", encoding="utf-8") as f:
                    f.write(f"{timestamp}--- 日志行数超标，已自动清除历史记录 ---\n")
                    f.write(msg + "\n") 
    except Exception as e:
        print(f"日志系统出错: {e}")

def load_config_json(path, default_value):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger(f"配置文件读取失败: {e}")
    return default_value

def save_data_to_json(data):
    try:
        with open(DATA_FILENAME, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logger(f"保存失败: {e}")
        return False

def get_file_content(path):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f: return f.read().strip()
        except: pass
    return ""

def parse_stores_config():
    raw_data = load_config_json(DATA_FILENAME, {})
    parsed_map = {}
    for region, stores_list in raw_data.items():
        region_stores = []
        for item in stores_list:
            store_obj = {
                "full_name": item.get("full_name", ""), "name": item.get("name", ""),
                "douyin": item.get("douyin", ""), "url": item.get("url", ""),
                "slots": [], "room_id": None, "live_consumption": "未开播", "live_cost": "-",
                # 【新增】：为大屏图表预留的大屏数据缓存
                "overview": {}, "trendPoints": []
            }
            time_str = str(item.get("time", ""))
            budgets = [b.strip() for b in str(item.get("budget", "")).split('/')]
            matches = re.findall(r'(\d{1,2}:\d{2})[^\d]*(\d{1,2}:\d{2})(?:\s*[\(（](.*?)[\)）])?', time_str)
            if matches:
                for i, (start, end, type_match) in enumerate(matches):
                    store_obj['slots'].append({
                        "start": start, "end": end, "type": type_match or "常规", 
                        "budget": budgets[i] if i < len(budgets) else (budgets[-1] if budgets else "")
                    })
            region_stores.append(store_obj)
        parsed_map[region] = region_stores
    return parsed_map

# ==========================================
# 核心网络请求 API (加入深度防崩溃容错机制)
# ==========================================
class DouyinAPI:
    def __init__(self, cookie):
        self.cookie = cookie
        self.session = requests.Session()
        self.headers = {
            "Authority": "www.autoengine.com", 
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.autoengine.com", 
            "Referer": "https://www.autoengine.com/live-screen", 
            "Content-Type": "application/json", 
            "jdc-saas-header-app-id": "22",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Cookie": self.cookie
        }

    def fetch_active_room_ids(self, open_ids):
        url = "https://www.autoengine.com/motor/dealer/jdc_saas/live/user/info?__method=window.fetch"
        try:
            response = self.session.post(url, json={"open_id_list": open_ids}, headers=self.headers, timeout=30)
            if response.status_code == 200:
                data = response.json()
                # 【核心修复】：安全地判断字典键值，防止 null 引发 NoneType iterable 崩溃
                if data and isinstance(data, dict):
                    inner_data = data.get("data")
                    if inner_data and isinstance(inner_data, dict) and "info_map" in inner_data:
                        return {re.sub(r'[（）\(\)\s]', '', v.get("nickname", "")): v.get("room_id") 
                                for _, v in inner_data["info_map"].items() if v.get("room_id") and str(v.get("room_id")) != "0"}
                    else:
                        # 记录被限流或未登录的具体原因，而不是直接闪退
                        error_msg = data.get("message", "未知异常结构")
                        logger(f"⚠️ 活跃列表接口异常 (通常为 Cookie 失效或被限流): {error_msg}")
            return None
        except Exception as e:
            logger(f"❌ 获取活跃列表出错: {e}")
            return None 

    def fetch_screen_overview(self, room_id):
        """获取直播间概览数据 (包含 17消耗、18成本，以及在线人数等)"""
        url = "https://www.autoengine.com/motor/dealer/jdc_saas/live/screen/overview/data?__method=window.fetch"
        try:
            response = self.session.post(url, json={"room_id": room_id, "is_private": False}, headers=self.headers, timeout=10)
            if response.status_code == 200:
                return response.json()
        except: pass
        return None

    def fetch_screen_trend(self, room_id):
        """获取直播间趋势曲线数据 (曝光、在线、进入)"""
        url = "https://www.autoengine.com/motor/dealer/jdc_saas/live/screen/trend/data?__method=window.fetch"
        try:
            response = self.session.post(url, json={"room_id": room_id, "is_private": False, "time_dimension": 0}, headers=self.headers, timeout=10)
            if response.status_code == 200:
                return response.json()
        except: pass
        return None

def fetch_ocean_data(region_name, keyword):
    accounts_map = db.ocean_accounts.get(region_name, {})
    cookie = get_file_content(OCEAN_COOKIE_FILENAME)
    if not accounts_map: return {"code": -1, "msg": "未找到账户配置"}
    if not cookie: return {"code": -1, "msg": "未找到 Cookie"}

    all_details = []
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    
    def query_task(acc_name, adv_id):
        url = f"https://ad.oceanengine.com/ad/api/promotion/projects/list?aadvid={adv_id}"
        headers = { 'content-type': 'application/json;charset=UTF-8', 'cookie': cookie,
                    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36' }
        payload = { "st": today, "et": today, "keyword": keyword, "project_status": [-1], 
                    "fields": ["stat_cost", "conversion_cost", "convert_cnt"], "limit": 50, "page": 1, "search_type": "8", "campaign_type": [1] }
        try: 
            # PC端使用原生 requests 增加超时，保持稳定性
            res = requests.post(url, headers=headers, json=payload, timeout=15)
            return acc_name, adv_id, res.json()
        except: 
            return acc_name, adv_id, None

    with ThreadPoolExecutor(max_workers=10) as executor:
        for f in as_completed([executor.submit(query_task, k, v) for k, v in accounts_map.items()]):
            name, aid, res = f.result()
            if res and res.get("code") == 0:
                for p in res.get("data", {}).get("projects", []):
                    if "不在投放时段" in p.get("project_status_second_name", []): continue
                    metrics, status = p.get("metrics") or {}, p.get("project_status")
                    try: c_val = float(str(metrics.get("stat_cost", 0)).replace(',',''))
                    except: c_val = 0.0
                    if status in [0, 7, 11] or c_val > 0:
                        status_tag = "项目超预算" if status == 7 else "组超预算" if status == 11 else "已暂停" if status != 0 else ""
                        all_details.append({ "account_name": name, "adv_id": aid, "project_name": p.get("project_name", "未知"),
                            "status_tag": status_tag, "cost": metrics.get("stat_cost", "0.00"), "conv_cost": metrics.get("conversion_cost", "0.00"),
                            "convert_cnt": metrics.get("convert_cnt", "0"), "plan_budget": p.get("campaign_budget", "0.00") })
    all_details.sort(key=lambda x: float(str(x['cost']).replace(',','')), reverse=True)
    return {"code": 0, "details": all_details}

# --- 后台工作线程 ---
def background_worker():
    logger("✅ 后台监控服务已启动 (PC端防闪退稳定版 + 大屏数据同步)")
    while True:
        try:
            db.ocean_accounts = load_config_json(OCEAN_ACCOUNTS_FILE, {})
            db.target_open_ids = load_config_json(TARGET_OPEN_IDS_FILE, [])
            cookie = get_file_content(COOKIE_FILENAME)
            
            if not cookie or not db.target_open_ids:
                logger("⏳ 等待 Cookie 或 OpenID 配置...")
                time.sleep(5)
                continue

            api = DouyinAPI(cookie)
            active_rooms_map = api.fetch_active_room_ids(db.target_open_ids)
            
            if active_rooms_map is None:
                # 记录失败但线程不中断，等待下一轮
                time.sleep(API_REFRESH_INTERVAL)
                continue

            new_stores_data = parse_stores_config()
            tasks = [store for stores in new_stores_data.values() for store in stores]

            def process_store(store):
                clean_name = re.sub(r'[（）\(\)\s]', '', store['name'])
                clean_full = re.sub(r'[（）\(\)\s]', '', store['full_name'])
                found_room_id = next((v for k, v in active_rooms_map.items() if clean_name in k or k in clean_name or clean_full in k), None)
                
                if found_room_id:
                    store['room_id'] = found_room_id
                    
                    # 1. 安全抓取大屏概览数据
                    overview_res = api.fetch_screen_overview(found_room_id)
                    if overview_res and overview_res.get("status") == 0:
                        o_data = overview_res.get("data") or {}
                        data_map = o_data.get("data_map") or {}
                        store['live_consumption'] = str(data_map.get("17", "0"))
                        store['live_cost'] = str(data_map.get("18", "0"))
                        store['overview'] = data_map
                    else:
                        store['live_consumption'] = "-"
                        store['live_cost'] = "-"
                        store['overview'] = {}

                    # 2. 安全抓取趋势曲线数据
                    trend_res = api.fetch_screen_trend(found_room_id)
                    if trend_res and trend_res.get("status") == 0:
                        t_data = trend_res.get("data") or {}
                        store['trendPoints'] = t_data.get("points", [])
                    else:
                        store['trendPoints'] = []
                else:
                    store['live_consumption'], store['live_cost'] = ("未开播", "-")
                    store['overview'] = {}
                    store['trendPoints'] = []
                return store

            with ThreadPoolExecutor(max_workers=16) as executor:
                for future in as_completed([executor.submit(process_store, s) for s in tasks]):
                    future.result() 

            with db.lock:
                db.stores_data = new_stores_data
                db.last_update = datetime.datetime.now()
            logger(f"数据刷新完成 | 在线直播间: {len(active_rooms_map)}")
        except Exception as e:
            logger(f"❌ 后台循环严重错误: {e}")
        time.sleep(API_REFRESH_INTERVAL)

# ==========================================
# FastAPI 生命周期与路由
# ==========================================
def open_browser_delayed():
    time.sleep(2)  
    open_url("http://127.0.0.1:8000/")

@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=background_worker, daemon=True).start()
    threading.Thread(target=open_browser_delayed, daemon=True).start()
    yield
    logger("服务已停止")

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/api/ocean/promotion")
async def get_ocean_api(region: str, keyword: str): return fetch_ocean_data(region, keyword)

@app.get("/api/data")
async def get_data():
    with db.lock: return {"data": db.stores_data, "last_update": db.last_update.strftime("%H:%M:%S")}

@app.get("/api/admin/raw_data")
async def get_raw_data(password: str = ""):
    if password != ADMIN_PASSWORD: raise HTTPException(status_code=403)
    return load_config_json(DATA_FILENAME, {})

@app.post("/api/admin/save_data")
async def save_raw_data(request: Request):
    body = await request.json()
    if body.get("password") != ADMIN_PASSWORD: raise HTTPException(status_code=403)
    if save_data_to_json(body.get("data")): return {"status": "success"}
    raise HTTPException(status_code=500)

@app.get("/", response_class=HTMLResponse)
async def serve_home():
    try:
        with open(get_resource_path("index.html"), "r", encoding="utf-8") as f: return f.read()
    except Exception as e: return f"Error: {e}"

@app.get("/admin", response_class=HTMLResponse)
async def serve_admin():
    try:
        with open(get_resource_path("admin.html"), "r", encoding="utf-8") as f: return f.read()
    except Exception as e: return f"Error: {e}"

@app.get("/ocean_accounts.json")
async def serve_ocean_accounts():
    p = os.path.join(os.path.dirname(__file__), OCEAN_ACCOUNTS_FILE)
    if os.path.exists(p):
        return FileResponse(p)
    return {}
# 兜底数据接口供前端调用
@app.get("/api/douyin/overview")
async def get_douyin_overview(room_id: str):
    cookie = get_file_content(COOKIE_FILENAME)
    if not cookie: return {"status": -1, "msg": "无 Cookie"}
    data = DouyinAPI(cookie).fetch_screen_overview(room_id)
    return data if data else {}

@app.get("/api/douyin/trend")
async def get_douyin_trend(room_id: str):
    cookie = get_file_content(COOKIE_FILENAME)
    if not cookie: return {"status": -1, "msg": "无 Cookie"}
    data = DouyinAPI(cookie).fetch_screen_trend(room_id)
    return data if data else {}

if __name__ == "__main__":
    if sys.platform == "win32":
        # Windows: Uvicorn 主线程，托盘子线程
        threading.Thread(target=setup_tray, daemon=True).start()
        toggle_console()
        uvicorn.run(app, host="0.0.0.0", port=8000, log_config=None)
    else:
        # macOS / Linux: 托盘必须在主线程，Uvicorn 放子线程
        threading.Thread(target=lambda: uvicorn.run(app, host="0.0.0.0", port=8000, log_config=None), daemon=True).start()
        setup_tray()  # pystray 的 icon.run() 会阻塞主线程维持程序运行
