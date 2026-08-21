#!/usr/bin/env python3
"""
FastAPI Server cho Coloring Book Generator (Lulu POD Ready Dashboard)
"""

import asyncio
import json
import logging
import os
import queue
import sys
import threading
from pathlib import Path
from typing import AsyncGenerator, Dict, Any, List

import yaml
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import main as book_main
from bookgen import pdf_builder

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("server")

app = FastAPI(title="Coloring Book Generator - Lulu Dashboard", version="3.0")

# CORS setup
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Auto-load .env file if present
ENV_FILE = ROOT / ".env"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.strip().strip('"').strip("'")

def find_chrome_exe() -> str | None:
    paths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe"),
    ]
    for p in paths:
        if os.path.exists(p):
            return p
    return None

def check_port_open(host="127.0.0.1", port=9222) -> bool:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        s.connect((host, port))
        s.close()
        return True
    except Exception:
        return False

def verify_gemini_api_key(api_key: str) -> tuple[bool, str]:
    if not api_key:
        return False, "Chưa nhập API Key"
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
    import urllib.request, urllib.error
    req = urllib.request.Request(url, headers={"User-Agent": "ColoringBookStudio/3.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            models = [m.get("name", "") for m in data.get("models", [])]
            return True, f"Kết nối thành công! Đã xác thực API key (tìm thấy {len(models)} models)."
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:200]
        return False, f"Lỗi HTTP {e.code}: {body}"
    except Exception as e:
        return False, f"Lỗi kết nối: {str(e)}"

# THÔNG SỐ IN LÀ DÙNG CHUNG, CHỈ ĐỘ RỘNG GÁY LÀ THEO TỪNG CUỐN.
# Gáy phụ thuộc số trang của chính cuốn đó và do nhà in cấp. Mọi khoá print khác
# mà lọt vào state.json sẽ đóng băng theo cuốn và làm sửa cấu hình chung vô hiệu.
PER_BOOK_PRINT = ("spine_width",)

# Active log listeners for SSE
tasks_logs: Dict[str, queue.Queue] = {}
single_gen_lock = threading.Lock()

# Lulu Presets
LULU_PRESETS = {
    "trim_sizes": [
        {"id": "us_letter", "name": "US Letter (8.5 x 11 in)", "width": 8.5, "height": 11.0, "popular": True},
        {"id": "square_small", "name": "Small Square (8.5 x 8.5 in)", "width": 8.5, "height": 8.5, "popular": True},
        {"id": "trade", "name": "Trade Paperback (6 x 9 in)", "width": 6.0, "height": 9.0, "popular": False},
        {"id": "square_medium", "name": "Square (8.25 x 8.25 in)", "width": 8.25, "height": 8.25, "popular": False},
        {"id": "a4", "name": "A4 (8.27 x 11.69 in)", "width": 8.27, "height": 11.69, "popular": False},
    ],
    "paper_types": [
        {"id": "60_white", "name": "60# White Paper (Standard)", "thickness": 0.00337, "desc": "0.003370 in/trang (do tu template Lulu) - Chuẩn ruột sách tô màu"},
        {"id": "60_cream", "name": "60# Cream Paper (Classic)", "thickness": 0.0025, "desc": "0.002500 in/trang - Giấy ngà truyền thống"},
        {"id": "70_white", "name": "70# White Coated (Premium)", "thickness": 0.0023, "desc": "0.002300 in/trang - Giấy bóng dày dặn"},
        {"id": "80_white", "name": "80# White Coated (Heavy)", "thickness": 0.003, "desc": "0.003000 in/trang - Giấy siêu dày cao cấp"},
    ],
    "binding_types": [
        {"id": "perfect", "name": "Perfect Bound (Bìa keo paperback)", "min_pages": 32, "desc": "Lulu yêu cầu tối thiểu 32 trang"},
        {"id": "coil", "name": "Coil Bound (Gáy xoắn nhựa)", "min_pages": 8, "desc": "Dễ lật mở 360 độ"},
        {"id": "saddle", "name": "Saddle Stitch (Ghim giữa)", "min_pages": 4, "desc": "Phù hợp sách ngắn dưới 48 trang"},
        {"id": "hardcover", "name": "Sách bìa cứng (Case Wrap)", "min_pages": 24, "desc": "Lề bao 0.875\" - độ rộng gáy do nhà in cấp"},
        {"id": "linen", "name": "Sách bìa cứng, bìa vải lanh (Linen)", "min_pages": 24, "desc": "Lề bao 0.875\" - độ rộng gáy do nhà in cấp"},
    ]
}


def book_title(cfg: dict) -> str:
    """Tiêu đề của cuốn ĐANG CHỌN, lấy từ state.json trước.

    config.yaml chỉ giữ tiêu đề của lần đồng bộ gần nhất. Endpoint nào đọc thẳng
    cfg["book"]["title"] sẽ dựng prompt bìa bằng tên cuốn KHÁC - đúng lỗi "sinh
    lại bìa sách rồng mà ra Forest Spirits".
    """
    try:
        st = book_main.load_state(book_main.paths_of(cfg)["state_file"])
        t = st.get("title") or (st.get("book") or {}).get("title")
        if t:
            return t
    except Exception:
        pass
    return cfg.get("book", {}).get("title", "")


def calculate_lulu_specs(cfg: dict) -> dict:
    """Tính toán chi tiết các thông số kỹ thuật cho Lulu POD"""
    p = cfg.get("print", {})
    b = cfg.get("book", {})
    
    trim_w = float(p.get("trim_width", 8.5))
    trim_h = float(p.get("trim_height", 11.0))
    bleed = float(p.get("bleed", 0.125))
    paper_thick = float(p.get("paper_thickness", 0.002252))
    num_images = int(b.get("num_images", 30))
    front_pages = 0
    blank_verso = bool(b.get("blank_verso", True))
    min_pages = int(p.get("min_pages", 32))

    # Tính tổng số trang interior
    # Mỗi hình = 1 trang. Nếu blank_verso = true -> 2 trang/hình
    interior_image_pages = num_images * (2 if blank_verso else 1)
    total_raw_pages = front_pages + interior_image_pages
    
    # Đảm bảo số trang chẵn và đạt min_pages của Lulu
    final_pages = total_raw_pages
    if final_pages < min_pages:
        final_pages = min_pages
    if final_pages % 2 != 0:
        final_pages += 1

    # Kích thước interior file (bao gồm bleed)
    interior_w_in = trim_w + (2 * bleed)
    interior_h_in = trim_h + (2 * bleed)

    # Kích thước bìa: hỏi ĐÚNG hàm mà build_cover dùng, đừng tính lại ở đây.
    # Bản trước tự tính bằng bleed nên dashboard báo 17.25x11.25 trong khi file
    # thật là 19.25x12.75 - người dùng tin vào con số sai rồi bị nhà in trả về.
    try:
        geo = pdf_builder.cover_geometry(p, final_pages)
        spine_in, cover_w_in, cover_h_in = geo["spine"], geo["total_w"], geo["total_h"]
        cover_note = geo["spec"]["label"]
    except Exception as e:  # thiếu độ rộng gáy bìa cứng -> báo thẳng lên UI
        spine_in = float(p.get("spine_width") or 0)
        cover_w_in = cover_h_in = 0.0
        cover_note = str(e).splitlines()[0]

    return {
        "calculated_pages": final_pages,
        "spine_width_in": round(spine_in, 4),
        "spine_width_mm": round(spine_in * 25.4, 2),
        "interior_size_in": f"{round(interior_w_in, 3)} x {round(interior_h_in, 3)}",
        "interior_px_300dpi": f"{int(interior_w_in * 300)} x {int(interior_h_in * 300)}",
        "cover_size_in": (f"{round(cover_w_in, 3)} x {round(cover_h_in, 3)}"
                          if cover_w_in else "—"),
        "cover_px_300dpi": (f"{int(cover_w_in * 300)} x {int(cover_h_in * 300)}"
                            if cover_w_in else "—"),
        "cover_note": cover_note,
        "lulu_compatible": final_pages >= min_pages and final_pages % 2 == 0,
    }


# ------------ API ENDPOINTS ------------

def sync_book_config(slug: str) -> dict:
    """Load config cho cuốn sách được chọn, đồng bộ thông tin từ state.json vào config.yaml"""
    config_file = ROOT / "config.yaml"
    cfg = book_main.load_cfg(config_file)
    cfg["_book"] = slug
    book_main.set_current_book(slug)
    
    P = book_main.paths_of(cfg)
    state = book_main.load_state(P["state_file"])
    
    # Extract stored per-book metadata from state
    book_meta = state.get("book", {})
    title = state.get("title") or book_meta.get("title") or slug.replace("-", " ").title()
    subtitle = state.get("subtitle") or book_meta.get("subtitle") or ""
    audience = state.get("audience") or book_meta.get("audience") or cfg.get("book", {}).get("audience", "kids")
    num_images = state.get("num_images") or book_meta.get("num_images") or cfg.get("book", {}).get("num_images", 30)
    blank_verso = state.get("blank_verso") if state.get("blank_verso") is not None else book_meta.get("blank_verso", True)

    cfg.setdefault("book", {})
    cfg["book"]["title"] = title
    cfg["book"]["subtitle"] = subtitle
    cfg["book"]["audience"] = audience
    cfg["book"]["num_images"] = num_images
    cfg["book"]["blank_verso"] = blank_verso
    
    DEFAULT_PRINT = {
        "trim_width": 8.5,
        "trim_height": 11.0,
        "bleed": 0.125,
        "safety_margin": 0.5,
        "gutter": 0.375,
        "dpi": 300,
        "paper_thickness": 0.00337,
        "binding": "perfect",
        "min_pages": 32,
        "cover_wrap": 0.25
    }
    
    # THÔNG SỐ IN LÀ DÙNG CHUNG, CHỈ ĐỘ RỘNG GÁY LÀ THEO TỪNG CUỐN.
    #
    # Bản trước cho cả khối state["print"] đè lên config.yaml. Mỗi cuốn vì thế
    # đóng băng một bản sao thông số in từ lúc nó được tạo, và sửa cấu hình toàn
    # cục KHÔNG lan sang cuốn cũ. Đã trả giá thật: cute-dog giữ spine_width 0.5
    # của đợt thử bìa cứng, dựng ra bìa rộng 17.75 trong khi cần 17.382, còn
    # paper_thickness thì vẫn là 0.002252 sai từ đầu.
    #
    # Độ rộng gáy thì ngược lại - nó phụ thuộc số trang của CHÍNH cuốn đó và do
    # nhà in cấp, nên bắt buộc phải giữ riêng.
    cfg_print = DEFAULT_PRINT.copy()
    if isinstance(cfg.get("print"), dict):
        cfg_print.update(cfg["print"])
    if isinstance(state.get("print"), dict):
        for k in PER_BOOK_PRINT:
            if state["print"].get(k) is not None:
                cfg_print[k] = state["print"][k]
    cfg["print"] = cfg_print

    if "subjects" in state:
        cfg["subjects"] = state["subjects"]
    else:
        cfg["subjects"] = []

    if "backend" in state:
        cfg["backend"] = state["backend"]

    if "process" in state:
        cfg["process"] = state["process"]

    # Write merged configuration back to config.yaml
    with open(config_file, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, sort_keys=False)

    # Save state back to state.json to ensure complete per-book state
    state["title"] = title
    state["subtitle"] = subtitle
    state["audience"] = audience
    state["num_images"] = num_images
    state["blank_verso"] = blank_verso
    state["book"] = cfg["book"]
    # Chỉ lưu lại phần THEO CUỐN. Ghi cả khối print vào state là cách cái bẫy
    # trên tự tái sinh: lần sau đọc lên nó lại đè config.
    state["print"] = {k: cfg.get("print", {}).get(k)
                      for k in PER_BOOK_PRINT
                      if cfg.get("print", {}).get(k) is not None}
    state["subjects"] = cfg.get("subjects", [])
    state["backend"] = cfg.get("backend", "api")
    state["process"] = cfg.get("process", {})
    book_main.save_state(P["state_file"], state)

    lulu_specs = calculate_lulu_specs(cfg)
    return {
        "status": "success",
        "active_book": slug,
        "config": cfg,
        "lulu_specs": lulu_specs
    }


# ------------ API ENDPOINTS ------------

@app.get("/api/lulu-presets")
def get_lulu_presets():
    return LULU_PRESETS


@app.get("/api/config")
def get_config():
    current_book = book_main.get_current_book()
    if not current_book:
        books = book_main.list_books()
        if books:
            current_book = books[0]
        else:
            current_book = "default-book"
    result = sync_book_config(current_book)
    return {
        "config": result["config"],
        "current_book": current_book,
        "lulu_specs": result["lulu_specs"]
    }


@app.post("/api/config")
def update_config(data: dict):
    config_file = ROOT / "config.yaml"
    existing_cfg = book_main.load_cfg(config_file)
    
    # Merge nested dictionaries
    def update_dict(d, u):
        for k, v in u.items():
            if isinstance(v, dict) and k in d and isinstance(d[k], dict):
                update_dict(d[k], v)
            else:
                d[k] = v
        return d

    new_cfg = update_dict(existing_cfg, data)
    
    with open(config_file, "w", encoding="utf-8") as f:
        yaml.dump(new_cfg, f, allow_unicode=True, sort_keys=False)
        
    slug = data.get("_book") or book_main.get_current_book()
    if slug:
        book_main.set_current_book(slug)
        # Also persist all metadata into output/books/<slug>/state.json
        P = book_main.paths_of(new_cfg)
        state = book_main.load_state(P["state_file"])
        if "book" in data:
            state.setdefault("book", {}).update(data["book"])
            if "title" in data["book"]: state["title"] = data["book"]["title"]
            if "subtitle" in data["book"]: state["subtitle"] = data["book"]["subtitle"]
            if "audience" in data["book"]: state["audience"] = data["book"]["audience"]
            if "num_images" in data["book"]: state["num_images"] = data["book"]["num_images"]
            if "blank_verso" in data["book"]: state["blank_verso"] = data["book"]["blank_verso"]
        if "print" in data:
            # CHỈ khoá theo-cuốn. Ghi cả khối print vào state là tự dựng lại cái
            # bẫy đóng băng: cuốn cũ giữ thông số in riêng, sửa cấu hình chung
            # không lan tới nó nữa.
            state["print"] = {k: data["print"][k] for k in PER_BOOK_PRINT
                              if data["print"].get(k) is not None}
        if "subjects" in data:
            state["subjects"] = data["subjects"]
        if "backend" in data:
            state["backend"] = data["backend"]
        if "process" in data:
            state["process"] = data["process"]
        book_main.save_state(P["state_file"], state)

    return {
        "status": "success",
        "lulu_specs": calculate_lulu_specs(new_cfg)
    }


# ------------ GEMINI AUTH & LOGIN ENDPOINTS ------------

@app.get("/api/gemini/profiles")
def get_gemini_profiles():
    config_file = ROOT / "config.yaml"
    cfg = book_main.load_cfg(config_file)
    browser = cfg.get("browser", {})
    
    profiles = browser.get("profiles", [])
    if not profiles and "user_data_dir" in browser:
        profiles = [browser["user_data_dir"]]
        
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    masked_key = f"{api_key[:6]}...{api_key[-4:]}" if len(api_key) >= 10 else ("***" if api_key else "")
    
    profile_details = []
    for p in profiles:
        p_path = Path(p).resolve()
        profile_details.append({
            "path": str(p_path),
            "name": p_path.name,
            "exists": p_path.exists()
        })
        
    return {
        "profiles": profile_details,
        "concurrency_per_profile": browser.get("concurrency_per_profile", browser.get("concurrency", 2)),
        "api": {
            "has_key": bool(api_key),
            "masked_key": masked_key
        }
    }


@app.post("/api/gemini/add-profile")
def add_gemini_profile(payload: dict):
    profile_name = payload.get("name", "").strip()
    if not profile_name:
        raise HTTPException(status_code=400, detail="Profile name required")
        
    config_file = ROOT / "config.yaml"
    cfg = book_main.load_cfg(config_file)
    browser = cfg.setdefault("browser", {})
    
    profiles = browser.get("profiles", [])
    if not profiles and "user_data_dir" in browser:
        profiles = [browser["user_data_dir"]]
        
    # sanitize profile name
    import re
    safe_name = re.sub(r'[^a-zA-Z0-9_\-]', '', profile_name)
    new_profile = f"./.chrome-{safe_name}"
    if new_profile not in profiles:
        profiles.append(new_profile)
        
    browser["profiles"] = profiles
    if "user_data_dir" in browser:
        del browser["user_data_dir"]
        
    if "concurrency" in browser:
        browser["concurrency_per_profile"] = browser["concurrency"]
        del browser["concurrency"]
        
    with open(config_file, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, sort_keys=False)
        
    return {"status": "success", "profiles": profiles}


@app.post("/api/gemini/delete-profile")
def delete_gemini_profile(payload: dict):
    profile_path = payload.get("path", "").strip()
    if not profile_path:
        raise HTTPException(status_code=400, detail="Profile path required")
        
    config_file = ROOT / "config.yaml"
    cfg = book_main.load_cfg(config_file)
    browser = cfg.setdefault("browser", {})
    profiles = browser.get("profiles", [])
    
    target_path = Path(profile_path).resolve()
    
    new_profiles = []
    for p in profiles:
        if Path(p).resolve() == target_path:
            continue
        new_profiles.append(p)
        
    browser["profiles"] = new_profiles
    
    with open(config_file, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, sort_keys=False)
        
    delete_files = payload.get("delete_files", True)
    if delete_files and target_path.exists():
        import shutil
        try:
            shutil.rmtree(target_path, ignore_errors=True)
        except Exception as e:
            logger.warning(f"Could not delete profile folder {target_path}: {e}")
            
    return {"status": "success", "profiles": new_profiles}


@app.post("/api/gemini/apikey")
def update_gemini_api_key(payload: dict):
    api_key = payload.get("api_key", "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="API Key không được để trống")

    # Verify key first
    is_valid, msg = verify_gemini_api_key(api_key)
    if not is_valid:
        return {"status": "error", "message": msg, "is_valid": False}

    # Save to environment
    os.environ["GEMINI_API_KEY"] = api_key

    # Save to .env file
    if ENV_FILE.exists():
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
        new_lines = []
        found = False
        for line in lines:
            if line.startswith("GEMINI_API_KEY="):
                new_lines.append(f'GEMINI_API_KEY="{api_key}"')
                found = True
            else:
                new_lines.append(line)
        if not found:
            new_lines.append(f'GEMINI_API_KEY="{api_key}"')
        env_content = "\n".join(new_lines)
    else:
        env_content = f'GEMINI_API_KEY="{api_key}"\n'

    ENV_FILE.write_text(env_content, encoding="utf-8")

    # Also update config.yaml
    config_file = ROOT / "config.yaml"
    if config_file.exists():
        cfg = book_main.load_cfg(config_file)
        cfg.setdefault("api", {})["key"] = api_key
        with open(config_file, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, allow_unicode=True, sort_keys=False)

    masked_key = f"{api_key[:6]}...{api_key[-4:]}"
    return {
        "status": "success",
        "message": msg,
        "is_valid": True,
        "masked_key": masked_key
    }


@app.post("/api/gemini/launch-chrome")
def launch_chrome(payload: dict = {}):
    chrome_path = find_chrome_exe()
    if not chrome_path:
        raise HTTPException(status_code=500, detail="Không tìm thấy chrome.exe trên máy tính này")

    profile_path = payload.get("profile_path")
    if not profile_path:
        config_file = ROOT / "config.yaml"
        cfg = book_main.load_cfg(config_file)
        browser = cfg.get("browser", {})
        profiles = browser.get("profiles", [])
        if not profiles and "user_data_dir" in browser:
            profiles = [browser["user_data_dir"]]
        
        if not profiles:
            profile_path = str((ROOT / ".chrome-profile").resolve())
        else:
            profile_path = str(Path(profiles[0]).resolve())

    profile_dir = Path(profile_path)
    profile_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        chrome_path,
        f"--user-data-dir={profile_dir.resolve()}",
        "https://gemini.google.com/app"
    ]

    try:
        import subprocess
        subprocess.Popen(cmd)
        return {
            "status": "success",
            "message": f"Đã mở cửa sổ Chrome cho {profile_dir.name}. Vui lòng đăng nhập Google!"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Không thể khởi chạy Chrome: {str(e)}")


@app.get("/api/books")
def list_books():
    books = book_main.list_books()
    current = book_main.get_current_book()
    details = []
    
    for b in books:
        base = book_main.BOOKS_DIR / b
        raw_count = len(list((base / "01_raw").glob("page_*.png"))) if (base / "01_raw").exists() else 0
        proc_count = len(list((base / "02_processed").glob("page_*.png"))) if (base / "02_processed").exists() else 0
        has_covers = (base / "01_raw" / "cover_front.png").exists() and (base / "01_raw" / "cover_back.png").exists()
        has_interior_pdf = (base / "03_pdf" / "interior.pdf").exists()
        has_cover_pdf = (base / "03_pdf" / "cover.pdf").exists()
        
        details.append({
            "slug": b,
            "is_active": b == current,
            "raw_count": raw_count,
            "proc_count": proc_count,
            "has_covers": has_covers,
            "has_interior_pdf": has_interior_pdf,
            "has_cover_pdf": has_cover_pdf
        })
        
    return {"books": details, "current": current}


@app.post("/api/books/select")
def select_book(payload: dict):
    slug = payload.get("slug")
    if not slug:
        raise HTTPException(status_code=400, detail="Missing book slug")
    return sync_book_config(slug)


@app.post("/api/books/create")
def create_book(payload: dict):
    title = payload.get("title", "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title required")
    
    slug = book_main.slugify(title)
    book_main.set_current_book(slug)
    
    cfg = book_main.load_cfg(ROOT / "config.yaml")
    cfg["book"]["title"] = title
    cfg["book"]["subtitle"] = payload.get("subtitle", "")
    cfg["book"]["audience"] = payload.get("audience", "kids")
    cfg["book"]["num_images"] = int(payload.get("num_images", 30))
    cfg["subjects"] = []
    cfg["_book"] = slug
    
    P = book_main.paths_of(cfg)
    P["raw_dir"].mkdir(parents=True, exist_ok=True)
    
    state = book_main.load_state(P["state_file"])
    state["title"] = title
    state["subtitle"] = payload.get("subtitle", "")
    state["audience"] = payload.get("audience", "kids")
    state["num_images"] = int(payload.get("num_images", 30))
    state["blank_verso"] = True
    state["book"] = cfg["book"]
    state["subjects"] = []
    book_main.save_state(P["state_file"], state)
    
    result = sync_book_config(slug)
    return {"status": "success", "slug": slug, "title": title, "config": result["config"], "lulu_specs": result["lulu_specs"]}


@app.post("/api/subjects/generate")
async def generate_subjects(payload: dict):
    cfg = book_main.load_cfg(ROOT / "config.yaml")
    title = payload.get("title") or cfg["book"]["title"]
    count = int(payload.get("count") or cfg["book"]["num_images"])
    
    _adj = book_main.audience_of(cfg)["adj"]
    prompt = (
        f'I am making a{"n" if _adj == "adult" else ""} {_adj} coloring book '
        f'titled "{title}". '
        f'Give me exactly {count} scene ideas. '
        "Each idea must be one short English sentence describing a single simple "
        "scene suitable for a bold-outline coloring page. "
        "Respond ONLY with a JSON array of strings, nothing else."
    )
    
    driver = book_main.make_driver(cfg)
    try:
        raw_resp = driver.ask_text(prompt)
        from bookgen.gemini_driver import parse_subject_list
        subjects = parse_subject_list(raw_resp, count)
        
        cfg["subjects"] = subjects
        with open(ROOT / "config.yaml", "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, allow_unicode=True, sort_keys=False)
            
        slug = book_main.get_current_book()
        if slug:
            P = book_main.paths_of(cfg)
            state = book_main.load_state(P["state_file"])
            state["subjects"] = subjects
            book_main.save_state(P["state_file"], state)
            
        return {"status": "success", "subjects": subjects}
    except Exception as e:
        logger.error(f"Error generating subjects: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class QueueLogHandler(logging.Handler):
    """Custom log handler to forward python logs to SSE queues"""
    def __init__(self, log_queue: queue.Queue):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record):
        msg = self.format(record)
        self.log_queue.put(msg)


class StreamToQueue:
    """Redirect sys.stdout and sys.stderr to SSE queue"""
    def __init__(self, log_queue: queue.Queue, original_stream):
        self.log_queue = log_queue
        self.original_stream = original_stream

    def write(self, buf):
        if self.original_stream:
            self.original_stream.write(buf)
        text = buf.strip()
        if text:
            for line in text.splitlines():
                if line.strip():
                    self.log_queue.put(line.strip())

    def flush(self):
        if self.original_stream:
            self.original_stream.flush()


@app.post("/api/tasks/run")
def run_task(payload: dict):
    from bookgen.cancel import reset_cancel
    reset_cancel()
    
    command = payload.get("command")
    if command not in ["generate", "process", "build", "check", "preview", "demo", "all"]:
        raise HTTPException(status_code=400, detail="Invalid command")

    task_id = f"task_{command}_{os.urandom(4).hex()}"
    log_q = queue.Queue()
    tasks_logs[task_id] = log_q

    def worker():
        handler = QueueLogHandler(log_q)
        formatter = logging.Formatter("%(asctime)s %(levelname)s: %(message)s", datefmt="%H:%M:%S")
        handler.setFormatter(formatter)
        
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)

        old_stdout = sys.stdout
        old_stderr = sys.stderr
        sys.stdout = StreamToQueue(log_q, old_stdout)
        sys.stderr = StreamToQueue(log_q, old_stderr)
        
        log_q.put(f"[START] Starting command '{command}'...")
        try:
            cfg = book_main.load_cfg(ROOT / "config.yaml")
            if command == "generate":
                book_main.cmd_generate(cfg)
            elif command == "process":
                book_main.cmd_process(cfg)
            elif command == "build":
                book_main.cmd_build(cfg)
            elif command == "check":
                book_main.cmd_check(cfg)
            elif command == "preview":
                book_main.cmd_preview(cfg)
            elif command == "demo":
                book_main.cmd_demo(cfg)
            elif command == "all":
                # Quy trình chuẩn = ĐÚNG 3 bước. `check` đã nằm trong build;
                # `preview` là việc marketing riêng, chạy bằng nút riêng.
                book_main.cmd_generate(cfg)
                book_main.cmd_process(cfg)
                book_main.cmd_build(cfg)
            log_q.put(f"[SUCCESS] Command '{command}' completed!")
        except Exception as e:
            logger.exception(f"Task failed: {e}")
            log_q.put(f"[ERROR] Task failed: {str(e)}")
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr
            root_logger.removeHandler(handler)
            log_q.put("[DONE]")

    threading.Thread(target=worker, daemon=True).start()
    return {"task_id": task_id, "command": command}


@app.post("/api/tasks/stop")
def stop_task():
    from bookgen.cancel import request_cancel
    request_cancel()
    logger.info("[STOP] User requested task cancellation!")
    return {"status": "success", "message": "Đã gửi lệnh DỪNG tới tất cả tiến trình đang chạy!"}


@app.get("/api/tasks/stream/{task_id}")
async def stream_task_logs(task_id: str):
    log_q = tasks_logs.get(task_id)
    if not log_q:
        raise HTTPException(status_code=404, detail="Task ID not found")

    async def event_generator():
        while True:
            try:
                while not log_q.empty():
                    msg = log_q.get()
                    if msg == "[DONE]":
                        yield "data: {\"type\": \"done\"}\n\n"
                        return
                    data_json = json.dumps({"type": "log", "message": msg})
                    yield f"data: {data_json}\n\n"
                await asyncio.sleep(0.3)
            except asyncio.CancelledError:
                break

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/api/gallery")
def get_gallery():
    cfg = book_main.load_cfg(ROOT / "config.yaml")
    P = book_main.paths_of(cfg)
    slug = cfg.get("_book") or book_main.get_current_book() or "default"
    
    raw_images = []
    if P["raw_dir"].exists():
        for f in sorted(P["raw_dir"].glob("*.png")):
            raw_images.append({
                "name": f.name,
                "url": f"/api/images/{slug}/01_raw/{f.name}",
                "size_kb": round(f.stat().st_size / 1024, 1)
            })

    proc_images = []
    if P["processed_dir"].exists():
        for f in sorted(P["processed_dir"].glob("*.png")):
            proc_images.append({
                "name": f.name,
                "url": f"/api/images/{slug}/02_processed/{f.name}",
                "size_kb": round(f.stat().st_size / 1024, 1)
            })

    pdfs = []
    if P["pdf_dir"].exists():
        for f in sorted(P["pdf_dir"].glob("*.pdf")):
            pdfs.append({
                "name": f.name,
                "url": f"/api/pdf/{slug}/{f.name}",
                "size_mb": round(f.stat().st_size / (1024 * 1024), 2)
            })

    return {
        "slug": slug,
        "raw_images": raw_images,
        "proc_images": proc_images,
        "pdfs": pdfs
    }


@app.get("/api/images/{slug}/{folder}/{filename}")
def serve_image(slug: str, folder: str, filename: str):
    if folder not in ["01_raw", "02_processed", "04_previews"]:
        raise HTTPException(status_code=400, detail="Invalid folder")
    path = book_main.BOOKS_DIR / slug / folder / filename
    if not path.exists():
        cfg = book_main.load_cfg(ROOT / "config.yaml")
        path = ROOT / cfg["paths"].get(f"{folder.replace('01_', '').replace('02_', '').replace('04_', '')}_dir", f"output/{folder}") / filename
        if not path.exists():
            raise HTTPException(status_code=404, detail="Image file not found")
    return FileResponse(path, media_type="image/png")


@app.get("/api/pdf/{slug}/{filename}")
def serve_pdf(slug: str, filename: str):
    path = book_main.BOOKS_DIR / slug / "03_pdf" / filename
    if not path.exists():
        cfg = book_main.load_cfg(ROOT / "config.yaml")
        path = ROOT / cfg["paths"]["pdf_dir"] / filename
        if not path.exists():
            raise HTTPException(status_code=404, detail="PDF file not found")
    return FileResponse(path, media_type="application/pdf", filename=filename)


# ------------ PREVIEW MARKETING ENDPOINTS ------------

@app.get("/api/previews/details")
def get_preview_details():
    cfg = book_main.load_cfg(ROOT / "config.yaml")
    P = book_main.paths_of(cfg)
    slug = cfg.get("_book") or book_main.get_current_book() or "default"
    title = cfg.get("book", {}).get("title", "Coloring Book")
    templates = cfg.get("prompts", {}).get("previews", {})
    
    preview_dir = P.get("preview_dir") or (P["raw_dir"].parent / "04_previews")
    raw_dir = P["raw_dir"]
    proc_dir = P["processed_dir"]
    attachments_map = book_main.get_preview_attachments_map(cfg)

    items = []
    for idx in range(1, 6):
        key = f"preview_{idx}"
        fname = f"{key}.png"
        fpath = preview_dir / fname
        has_file = fpath.exists()
        raw_prompt = templates.get(key, f"Mockup preview {idx}")
        prompt = raw_prompt.format(title=title)
        
        att_files = attachments_map.get(key, [])
        att_list = []
        for att_path in att_files:
            if att_path and att_path.exists():
                folder_name = "02_processed" if "02_processed" in str(att_path) or proc_dir in att_path.parents else "01_raw"
                att_fname = att_path.name
                
                if "cover" in att_fname.lower():
                    label = "Ảnh bìa trước (Front Cover)"
                elif "page_" in att_fname.lower():
                    p_num = att_fname.lower().replace("page_", "").split(".")[0]
                    label = f"Trang ruột #{p_num}"
                else:
                    label = att_fname
                    
                att_list.append({
                    "filename": att_fname,
                    "label": label,
                    "url": f"/api/images/{slug}/{folder_name}/{att_fname}?v={int(att_path.stat().st_mtime)}"
                })

        items.append({
            "key": key,
            "filename": fname,
            "index": idx,
            "has_file": has_file,
            "url": f"/api/images/{slug}/04_previews/{fname}?v={int(fpath.stat().st_mtime)}" if has_file else None,
            "prompt": prompt,
            "attachments": att_list,
            "size_kb": round(fpath.stat().st_size / 1024, 1) if has_file else 0
        })
        
    return {"status": "success", "items": items, "title": title}


@app.post("/api/previews/regenerate-single")
def regenerate_preview_single(payload: dict):
    key = payload.get("key")
    custom_prompt = payload.get("prompt")
    if not key:
        raise HTTPException(status_code=400, detail="Missing preview key")
        
    cfg = book_main.load_cfg(ROOT / "config.yaml")
    P = book_main.paths_of(cfg)
    raw_dir = P["raw_dir"]
    preview_dir = P.get("preview_dir") or (raw_dir.parent / "04_previews")
    preview_dir.mkdir(parents=True, exist_ok=True)
    
    title = cfg.get("book", {}).get("title", "Coloring Book")
    if custom_prompt:
        templates = cfg.get("prompts", {}).get("previews", {})
        templates[key] = custom_prompt
        cfg.setdefault("prompts", {})["previews"] = templates
        with open(ROOT / "config.yaml", "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, allow_unicode=True, sort_keys=False)
    else:
        templates = cfg.get("prompts", {}).get("previews", {})
        custom_prompt = templates.get(key, f"Coloring book mockup {key}").format(title=title)
        
    # Lấy các ảnh thực tế cần đính kèm (dùng helper thống nhất)
    attachments_map = book_main.get_preview_attachments_map(cfg)
    attach = attachments_map.get(key, [])
    # Giữ nguyên file cũ cho tới khi sinh xong file mới thành công, không xóa sớm
    dest = preview_dir / f"{key}.png"
    
    with single_gen_lock:
        from bookgen.cancel import reset_cancel
        reset_cancel()
        ok = book_main.generate_single(cfg, key, custom_prompt, dest, attach)
        if not ok:
            raise HTTPException(status_code=500, detail=f"Không sinh được {key}")
        book_main.finalize_preview(cfg, dest)
            
    slug = cfg.get("_book") or book_main.get_current_book() or "default"
    mtime = int(dest.stat().st_mtime) if dest.exists() else 0
    return {
        "status": "success",
        "key": key,
        "url": f"/api/images/{slug}/04_previews/{key}.png?v={mtime}"
    }


# ------------ RAW INSPECTOR ENDPOINTS ------------

@app.get("/api/raw-inspector/details")
def get_raw_inspector_details():
    cfg = book_main.load_cfg(ROOT / "config.yaml")
    P = book_main.paths_of(cfg)
    slug = cfg.get("_book") or book_main.get_current_book() or "default"
    state = book_main.load_state(P["state_file"])
    reviews = state.get("image_reviews", {})
    subjects = state.get("subjects") or cfg.get("subjects", [])

    # SỐ TRANG LẤY TỪ STATE CỦA CUỐN ĐANG CHỌN, không lấy từ config.yaml.
    # config.yaml giữ giá trị của lần đồng bộ gần nhất; cuốn 48 trang mà config
    # còn 24 thì Inspector giấu mất 24 ảnh, không sinh lại được ảnh nào trong đó.
    num_images = int(state.get("num_images")
                     or cfg.get("book", {}).get("num_images", 30))

    raw_files = {}
    if P["raw_dir"].exists():
        for f in P["raw_dir"].glob("*.png"):
            raw_files[f.stem] = f

    # ĐỪNG GIẤU ẢNH ĐÃ CÓ: nếu trên đĩa còn page_ lớn hơn num_images thì mở rộng
    # danh sách cho đủ, thà thừa ô trống còn hơn thiếu ảnh thật.
    for stem in raw_files:
        if stem.startswith("page_"):
            try:
                num_images = max(num_images, int(stem.split("_")[1]))
            except (IndexError, ValueError):
                pass

    proc_files = set()
    if P["processed_dir"].exists():
        for f in P["processed_dir"].glob("*.png"):
            proc_files.add(f.stem)

    items = []
    
    # Interior pages
    for i in range(1, num_images + 1):
        key = f"page_{i:03d}"
        fname = f"{key}.png"
        raw_f = raw_files.get(key)
        has_raw = raw_f is not None
        has_proc = key in proc_files
        
        subj = subjects[i-1] if i-1 < len(subjects) else f"Cảnh trang ruột số {i}"
        status = reviews.get(key, "pending" if has_raw else "missing")
        
        items.append({
            "key": key,
            "name": fname,
            "index": i,
            "type": "page",
            "subject": subj,
            "has_raw": has_raw,
            "raw_url": f"/api/images/{slug}/01_raw/{fname}" if has_raw else None,
            "has_proc": has_proc,
            "proc_url": f"/api/images/{slug}/02_processed/{fname}" if has_proc else None,
            "status": status,
            "size_kb": round(raw_f.stat().st_size / 1024, 1) if has_raw else 0,
            "mtime": raw_f.stat().st_mtime if has_raw else 0,
        })
        
    # Covers
    title = book_title(cfg)
    style = book_main.cover_style_of(cfg)
    for key, title_label in [("cover_front", "Bìa trước (Front Cover)"), ("cover_back", "Bìa sau (Back Cover)")]:
        fname = f"{key}.png"
        raw_f = raw_files.get(key)
        has_raw = raw_f is not None
        has_proc = key in proc_files
        status = reviews.get(key, "pending" if has_raw else "missing")
        
        tpl_key = "front_cover" if key == "cover_front" else "back_cover"
        saved = (state.get("prompts") or {}).get(f"{tpl_key}_custom")
        if saved:
            subj = saved
        else:
            try:
                subj = cfg["prompts"][tpl_key].format(
                    title=book_main.cover_title(cfg), style=style,
                    safe_pct=book_main.cover_safe_pct(cfg),
                    **book_main.cover_prompt_extras(cfg))
            except Exception:
                subj = cfg.get("prompts", {}).get(tpl_key, "")

        items.append({
            "key": key,
            "name": fname,
            "index": None,
            "type": "cover",
            "subject": subj,
            "has_raw": has_raw,
            "raw_url": f"/api/images/{slug}/01_raw/{fname}" if has_raw else None,
            "has_proc": has_proc,
            "proc_url": f"/api/images/{slug}/02_processed/{fname}" if has_proc else None,
            "status": status,
            "size_kb": round(raw_f.stat().st_size / 1024, 1) if has_raw else 0,
            "mtime": raw_f.stat().st_mtime if has_raw else 0,
        })
        
    summary = {
        "total": len(items),
        "has_raw": sum(1 for x in items if x["has_raw"]),
        "approved": sum(1 for x in items if x["status"] == "approved"),
        "needs_review": sum(1 for x in items if x["status"] == "needs_review"),
        "rejected": sum(1 for x in items if x["status"] == "rejected"),
        "pending": sum(1 for x in items if x["status"] == "pending"),
    }
    
    return {
        "slug": slug,
        "items": items,
        "summary": summary
    }


@app.post("/api/raw-inspector/update-status")
def update_inspector_status(payload: dict):
    key = payload.get("key")
    status = payload.get("status")
    if not key or not status:
        raise HTTPException(status_code=400, detail="Key và status là bắt buộc")
        
    cfg = book_main.load_cfg(ROOT / "config.yaml")
    P = book_main.paths_of(cfg)
    state = book_main.load_state(P["state_file"])
    state.setdefault("image_reviews", {})[key] = status
    book_main.save_state(P["state_file"], state)
    return {"status": "success", "key": key, "review_status": status}


@app.post("/api/raw-inspector/update-subject")
def update_inspector_subject(payload: dict):
    key = payload.get("key")
    subject = payload.get("subject", "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="Key là bắt buộc")
        
    cfg = book_main.load_cfg(ROOT / "config.yaml")
    P = book_main.paths_of(cfg)
    state = book_main.load_state(P["state_file"])
    
    if key.startswith("page_"):
        try:
            idx = int(key.replace("page_", "")) - 1
            # seed tu state cua cuon nay, khong tu config.yaml (cua cuon khac)
            subjects = list(state.get("subjects") or cfg.get("subjects") or [])
            while len(subjects) <= idx:
                subjects.append("")
            subjects[idx] = subject
            cfg["subjects"] = subjects
            state["subjects"] = subjects
            
            with open(ROOT / "config.yaml", "w", encoding="utf-8") as f:
                yaml.dump(cfg, f, allow_unicode=True, sort_keys=False)
            book_main.save_state(P["state_file"], state)
        except ValueError:
            pass
    elif key in ("cover_front", "cover_back"):
        # Theo TỪNG CUỐN. Lưu toàn cục thì mọi cuốn sau đều dính prompt của cuốn
        # đầu tiên, kèm cả tiêu đề cũ đã nướng cứng vào text.
        tpl_key = "front_cover" if key == "cover_front" else "back_cover"

        # CHỈ LƯU KHI NGƯỜI DÙNG THẬT SỰ SỬA.
        #
        # Inspector hiển thị prompt ĐÃ THAY CHỖ (tiêu đề, chủ đề đã ghép vào
        # text). Bản trước lưu thẳng những gì hiện trên ô nhập, nên chỉ cần mở
        # Inspector rồi bấm lưu là bản chụp đó đông cứng lại - kể cả khi người
        # dùng không gõ gì. Tiêu đề của cuốn đang xem lúc đó bị khoá vào prompt,
        # và cuốn khác sinh lại bìa vẫn ra tên cũ.
        try:
            default = cfg["prompts"][tpl_key].format(
                title=book_main.cover_title(cfg),
                style=book_main.cover_style_of(cfg),
                safe_pct=book_main.cover_safe_pct(cfg),
                **book_main.cover_prompt_extras(cfg))
        except Exception:
            default = None

        prompts = state.setdefault("prompts", {})
        if default is not None and " ".join(subject.split()) == " ".join(default.split()):
            prompts.pop(f"{tpl_key}_custom", None)      # giống mẫu -> không phải tuỳ chỉnh
        else:
            prompts[f"{tpl_key}_custom"] = subject
        book_main.save_state(P["state_file"], state)
            
    return {"status": "success", "key": key, "subject": subject}


@app.post("/api/raw-inspector/replace")
async def replace_inspector_image(key: str = Form(...), file: UploadFile = File(...)):
    if not key:
        raise HTTPException(status_code=400, detail="Thiếu key")
        
    cfg = book_main.load_cfg(ROOT / "config.yaml")
    P = book_main.paths_of(cfg)
    P["raw_dir"].mkdir(parents=True, exist_ok=True)
    
    dest = P["raw_dir"] / f"{key}.png"
    content = await file.read()
    
    from io import BytesIO
    from PIL import Image
    try:
        img = Image.open(BytesIO(content))
        img.save(dest, "PNG")
    except Exception:
        with open(dest, "wb") as f:
            f.write(content)
            
    state = book_main.load_state(P["state_file"])
    if key not in state.get("done", []):
        state.setdefault("done", []).append(key)
    state.setdefault("image_reviews", {})[key] = "approved"
    book_main.save_state(P["state_file"], state)
    
    return {"status": "success", "key": key, "message": f"Đã thay thế ảnh {key}.png thành công!"}


@app.post("/api/raw-inspector/delete")
def delete_inspector_image(payload: dict):
    key = payload.get("key")
    if not key:
        raise HTTPException(status_code=400, detail="Thiếu key")
        
    cfg = book_main.load_cfg(ROOT / "config.yaml")
    P = book_main.paths_of(cfg)
    
    raw_file = P["raw_dir"] / f"{key}.png"
    proc_file = P["processed_dir"] / f"{key}.png"
    
    if raw_file.exists():
        raw_file.unlink()
    if proc_file.exists():
        proc_file.unlink()
        
    state = book_main.load_state(P["state_file"])
    if key in state.get("done", []):
        state["done"].remove(key)
    if "image_reviews" in state and key in state["image_reviews"]:
        del state["image_reviews"][key]
    book_main.save_state(P["state_file"], state)
    
    return {"status": "success", "key": key, "message": f"Đã xóa ảnh {key}"}


@app.post("/api/raw-inspector/process-single")
def process_inspector_image_single(payload: dict):
    key = payload.get("key")
    if not key:
        raise HTTPException(status_code=400, detail="Thiếu key")
        
    cfg = book_main.load_cfg(ROOT / "config.yaml")
    P = book_main.paths_of(cfg)
    
    raw_file = P["raw_dir"] / f"{key}.png"
    if not raw_file.exists():
        raise HTTPException(status_code=404, detail=f"Không tìm thấy ảnh raw {key}.png")
        
    P["processed_dir"].mkdir(parents=True, exist_ok=True)
    proc_file = P["processed_dir"] / f"{key}.png"
    
    p = cfg.get("print", {})
    dpi = int(p.get("dpi", 300))
    bleed = float(p.get("bleed", 0.125))
    trim_w = float(p.get("trim_width", 8.5))
    trim_h = float(p.get("trim_height", 11.0))
    w_px = int((trim_w + 2 * bleed) * dpi)
    h_px = int((trim_h + 2 * bleed) * dpi)
    
    pr = cfg.get("process") or {}
    
    from bookgen import imaging
    if key.startswith("cover_"):
        # Bìa trước: nếp gấp gáy bên trái; bìa sau: bên phải.
        spine_side = "right" if key == "cover_back" else "left"
        pr_cfg = cfg.get("print") or {}
        book_main.finalize_cover(cfg, raw_file)   # xoá dấu sao trên raw trước
        insets = pdf_builder.cover_art_insets(pr_cfg, spine_side)
        imaging.prepare_cover_art(raw_file, proc_file, w_px, h_px,
                                  insets, str(pr_cfg.get("cover_fill", "blur")))
    else:
        imaging.process_lineart(
            raw_file, proc_file, w_px, h_px,
            threshold=int(pr.get("threshold", 165)),
            pure_bw=book_main.effective_pure_bw(cfg),
            sharpen=bool(pr.get("sharpen", True)),
        )
        
    slug = cfg.get("_book") or book_main.get_current_book() or "default"
    return {
        "status": "success",
        "key": key,
        "proc_url": f"/api/images/{slug}/02_processed/{key}.png"
    }


@app.post("/api/raw-inspector/regenerate-single")
def regenerate_inspector_image_single(payload: dict):
    key = payload.get("key")
    custom_prompt = payload.get("prompt")
    if not key:
        raise HTTPException(status_code=400, detail="Thiếu key")
        
    cfg = book_main.load_cfg(ROOT / "config.yaml")
    P = book_main.paths_of(cfg)
    P["raw_dir"].mkdir(parents=True, exist_ok=True)
    dest = P["raw_dir"] / f"{key}.png"
    
    title = book_title(cfg)
    
    if key.startswith("page_"):
        try:
            idx = int(key.replace("page_", ""))
        except ValueError:
            idx = 1
            
        # NGUỒN CHÂN LÝ LÀ state.json CỦA CUỐN ĐANG CHỌN, không phải config.yaml.
        # config.yaml giữ subjects của cuốn làm GẦN NHẤT, nên đọc từ đó thì bấm
        # sinh lại một trang trong sách rồng sẽ vẽ ra cảnh mèo của cuốn trước.
        state = book_main.load_state(P["state_file"])
        subjects = list(state.get("subjects") or cfg.get("subjects") or [])

        if custom_prompt:
            subj = custom_prompt
            while len(subjects) < idx:
                subjects.append("")
            subjects[idx - 1] = subj

            state["subjects"] = subjects
            book_main.save_state(P["state_file"], state)

            # Ghi cả vào config.yaml để giao diện hiện đúng ngay, nhưng state mới
            # là thứ quyết định (sync_book_config cho state đè lên config).
            cfg["subjects"] = subjects
            with open(ROOT / "config.yaml", "w", encoding="utf-8") as f:
                yaml.dump(cfg, f, allow_unicode=True, sort_keys=False)
        else:
            subj = subjects[idx - 1] if idx - 1 < len(subjects) else f"scene {idx}"

        if subj.lower().startswith("a black and white") or "coloring page illustration" in subj.lower():
            prompt = subj
        else:
            try:
                prompt = book_main.format_page_prompt(cfg, subj)
            except Exception:
                _desc = book_main.audience_of(cfg)["desc"]
                prompt = f"A black and white line-art coloring page illustration for {_desc}. Subject: {subj}."

    elif key in ("cover_front", "cover_back"):
        # PROMPT TUỲ CHỈNH LƯU THEO TỪNG CUỐN, KHÔNG LƯU TOÀN CỤC.
        #
        # Bản trước ghi vào cfg["prompts"]["front_cover_custom"] rồi dump ra
        # config.yaml, và nhánh else lấy nó ra TRƯỚC prompt thật. Hậu quả: gõ
        # prompt tuỳ chỉnh đúng một lần cho một cuốn là từ đó MỌI cuốn đều dùng
        # bản chụp đông cứng đó - kèm tên sách cũ đã nướng thẳng vào text, nên
        # mọi bìa sinh sau đều mang tiêu đề của cuốn đầu tiên.
        tpl_key = "front_cover" if key == "cover_front" else "back_cover"
        state = book_main.load_state(P["state_file"])
        saved = (state.get("prompts") or {}).get(f"{tpl_key}_custom")

        if custom_prompt:
            prompt = custom_prompt
            state.setdefault("prompts", {})[f"{tpl_key}_custom"] = custom_prompt
            book_main.save_state(P["state_file"], state)
        elif saved:
            prompt = saved
        else:
            style = book_main.cover_style_of(cfg)
            prompt = cfg["prompts"][tpl_key].format(
                title=book_main.cover_title(cfg), style=style,
                safe_pct=book_main.cover_safe_pct(cfg),
                **book_main.cover_prompt_extras(cfg))

    attach = []
    if key == "cover_back":
        front_file = P["raw_dir"] / "cover_front_titled.png"
        if not front_file.exists():
            front_file = P["raw_dir"] / "cover_front.png"
        if front_file.exists():
            attach = [front_file]
            
    # Giữ nguyên file cũ cho tới khi sinh xong file mới thành công, không xóa sớm

    with single_gen_lock:
        from bookgen.cancel import reset_cancel
        reset_cancel()
        ok = book_main.generate_single(cfg, key, prompt, dest, attach)
        if not ok:
            raise HTTPException(status_code=500, detail="Gemini sinh ảnh thất bại. Kiểm tra kết nối / API Key / Chrome.")
            
    state = book_main.load_state(P["state_file"])
    if key not in state.get("done", []):
        state.setdefault("done", []).append(key)
    state.setdefault("image_reviews", {})[key] = "approved"
    book_main.save_state(P["state_file"], state)
    
    slug = cfg.get("_book") or book_main.get_current_book() or "default"
    import time
    ts = int(time.time())
    return {
        "status": "success",
        "key": key,
        "raw_url": f"/api/images/{slug}/01_raw/{key}.png?v={ts}"
    }


# --------------------------------------------------------------- batch nhiều cuốn

from bookgen.batch import BatchRunner  # noqa: E402

batch_runner = BatchRunner(ROOT, book_main)
batch_runner.load_from_disk()


@app.post("/api/batch/start")
def batch_start(payload: dict):
    """Nhận một list chủ đề -> tự dựng ra từng cuốn sách.

    Config lấy từ config.yaml tại thời điểm bấm chạy và dùng chung cho cả batch.
    """
    raw = payload.get("titles") or ""
    titles = raw if isinstance(raw, list) else raw.splitlines()
    try:
        return batch_runner.start(titles, payload.get("num_images"))
    except (RuntimeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/batch/resume")
def batch_resume():
    """Chạy tiếp các cuốn còn dở, giữ nguyên slug + config + ảnh đã vẽ."""
    try:
        return batch_runner.resume()
    except (RuntimeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/batch/status")
def batch_status():
    return batch_runner.status()


@app.post("/api/batch/stop")
def batch_stop():
    return batch_runner.stop()


@app.get("/api/batch/log/{slug}")
def batch_log(slug: str, tail: int = 200):
    f = book_main.BOOKS_DIR / slug / "run.log"
    if not f.exists():
        return {"slug": slug, "lines": []}
    lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
    return {"slug": slug, "lines": lines[-tail:]}


# Static directory setup
static_dir = ROOT / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=True)
