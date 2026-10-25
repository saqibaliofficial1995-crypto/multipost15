"""
Video metadata generation with Groq vision (keyframes) and Gemini Files API, with strict validation.
"""
import socket

_orig = socket.getaddrinfo

def _ipv4(host, port, family=0, type=0, proto=0, flags=0):
    if family == 0 or family == socket.AF_UNSPEC:
        family = socket.AF_INET
    return _orig(host, port, family, type, proto, flags)

socket.getaddrinfo = _ipv4

import base64
import json
import os
import re
import subprocess
import tempfile
import time
import requests

CURRENCY_PATTERN = re.compile(r"[\$\u20ac\u00a3\u00a5\u20b9]| (USD|AED|EUR|GBP|INR|PKR) ", re.IGNORECASE)
MAGNITUDE_PATTERN = re.compile(r" \d+([.,]\d+)?\s*(m|b|k|million|billion|sqft|sq\s*ft|feet) ", re.IGNORECASE)

def clean_text_field(text):
    t = CURRENCY_PATTERN.sub("", text)
    t = MAGNITUDE_PATTERN.sub("", t)
    return re.sub(r"\s+", " ", t).strip()

def sanitize_metadata(meta, duration, channel_name="Maureen Farr"):
    # Title < 60 chars
    title = clean_text_field(meta.get("title", "Inspiring Moment"))
    if len(title) > 59:
        title = title[:59].rsplit(" ", 1)[0]
    if not title:
        title = "Inspiring Moment"

    # Tags: 10-15 tags, total string < 460
    raw_tags = meta.get("tags", [])
    if isinstance(raw_tags, str):
        raw_tags = [t.strip() for t in raw_tags.split(",") if t.strip()]

    clean_tags = []
    for tag in raw_tags:
        ct = clean_text_field(tag)
        if ct and len(ct) > 1 and ct not in clean_tags:
            clean_tags.append(ct)

    # Ensure 10-15 tags
    fallback_tags = ["lifestyle", "shorts", "daily", "inspiration", "trends", "video", "moments", "viral", "creative", "explore", "focus", "mindset"]
    for ft in fallback_tags:
        if len(clean_tags) >= 12:
            break
        if ft not in clean_tags:
            clean_tags.append(ft)

    clean_tags = clean_tags[:15]
    while len(",".join(clean_tags)) > 450 and len(clean_tags) > 10:
        clean_tags.pop()

    # Hashtags: exactly 3 hashtags, #shorts when duration <= 60
    raw_hashtags = meta.get("hashtags", [])
    if isinstance(raw_hashtags, str):
        raw_hashtags = [h.strip() for h in raw_hashtags.split() if h.strip()]

    clean_ht = []
    for h in raw_hashtags:
        h = h.strip()
        if not h.startswith("#"):
            h = f"#{h}"
        h = re.sub(r"[^#a-zA-Z0-9]", "", h)
        if len(h) > 1 and h.lower() not in [x.lower() for x in clean_ht]:
            clean_ht.append(h)

    if duration <= 60 and not any(h.lower() == "#shorts" for h in clean_ht):
        clean_ht.insert(0, "#shorts")

    default_hts = ["#shorts", "#lifestyle", "#viral", "#inspire"]
    for dh in default_hts:
        if len(clean_ht) >= 3:
            break
        if not any(h.lower() == dh.lower() for h in clean_ht):
            clean_ht.append(dh)

    clean_ht = clean_ht[:3]

    # Description = body + blank line + 3 hashtags + blank line + channel_name
    body = clean_text_field(meta.get("description", ""))
    if not body:
        body = f"{title}. Watch till the end for something special!"

    ht_str = " ".join(clean_ht)
    description = f"{body}\n\n{ht_str}\n\n{channel_name}"

    meta["title"] = title
    meta["description"] = description
    meta["tags"] = clean_tags
    meta["hashtags"] = clean_ht
    return meta

def extract_keyframes(video_path, num_frames=3, duration_seconds=60):
    temp_dir = tempfile.mkdtemp(prefix="groq_frames_")
    b64_frames = []
    try:
        dur = max(1.0, float(duration_seconds))
        fractions = [0.2, 0.5, 0.8] if num_frames == 3 else [0.25, 0.5, 0.75]
        for i, frac in enumerate(fractions):
            ts = max(0.1, dur * frac)
            frame_path = os.path.join(temp_dir, f"frame_{i}.jpg")
            cmd = ["ffmpeg", "-y", "-ss", f"{ts:.2f}", "-i", video_path, "-vframes", "1", "-q:v", "2", frame_path]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            if os.path.exists(frame_path):
                with open(frame_path, "rb") as f:
                    b64_frames.append(base64.b64encode(f.read()).decode("utf-8"))
    finally:
        for f in os.listdir(temp_dir):
            try: os.remove(os.path.join(temp_dir, f))
            except: pass
        try: os.rmdir(temp_dir)
        except: pass
    return b64_frames

def get_best_groq_vision_model(api_key, fallback_model="qwen/qwen3.8-27b"):
    """
    GROQ MODEL AUTO-SELECT: Query live models endpoint at runtime,
    pick a live vision-capable model, and fall back to configured model if query fails.
    """
    url = "https://api.groq.com/openai/v1/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            models_data = resp.json().get("data", [])
            # Priority candidates for vision on Groq
            active_models = [m["id"] for m in models_data if m.get("active", True)]
            candidates = [
                m for m in active_models
                if "vision" in m.lower() or "qwen" in m.lower()
            ]
            preferred_order = [
                "meta-llama/llama-3.2-11b-vision-preview",
                "llama-3.2-11b-vision-preview",
                "meta-llama/llama-3.2-90b-vision-preview",
                "llama-3.2-90b-vision-preview",
                "qwen/qwen3.8-27b",
            ]
            for pref in preferred_order:
                if pref in candidates:
                    print(f"[groq] Auto-selected live vision model: {pref}", flush=True)
                    return pref
            if candidates:
                chosen = candidates[0]
                print(f"[groq] Auto-selected live vision model: {chosen}", flush=True)
                return chosen
    except Exception as e:
        print(f"[groq] Warning querying live models endpoint: {e}", flush=True)

    print(f"[groq] Using configured fallback model: {fallback_model}", flush=True)
    return fallback_model

def generate_video_metadata_groq(video_path, duration, api_key, model="qwen/qwen3.8-27b"):
    # Auto-select active vision model
    selected_model = get_best_groq_vision_model(api_key, fallback_model=model)

    b64_frames = extract_keyframes(video_path, num_frames=3, duration_seconds=duration)
    if not b64_frames:
        raise RuntimeError("Failed to extract keyframes with ffmpeg.")

    prompt = (
        "Watch these 3 video frames carefully. Do NOT use the video filename to guess content.\n"
        "Return ONLY a raw JSON object with no markdown fences, no formatting before or after.\n"
        "Required JSON keys:\n"
        "- title: engaging YouTube title, strictly under 60 characters, no currency symbols or number claims.\n"
        "- description: 2-3 engaging sentences describing what is happening visually in the video.\n"
        "- tags: array of 10 to 15 relevant keyword tags (single words or short phrases).\n"
        "- hashtags: array of exactly 3 relevant hashtags starting with '#' (include #shorts if short/vertical).\n"
        "- topic: a specific 1 to 4 word thematic niche/topic of this video inferred strictly from the video visuals.\n"
        "CRITICAL RULES: Do NOT include currency ($ € £ ¥ ₹) or monetary words. "
        "Do NOT mention sizes or magnitude claims. The 'topic' field is strictly MANDATORY."
    )

    content = [{"type": "text", "text": prompt}]
    for b64_img in b64_frames:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64_img}"}
        })

    payload = {
        "model": selected_model,
        "messages": [
            {"role": "user", "content": content}
        ],
        "temperature": 0.2,
        "max_tokens": 1000,
        "response_format": {"type": "json_object"}
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    url = "https://api.groq.com/openai/v1/chat/completions"
    res = None
    for retry_i in range(3):
        try:
            res = requests.post(url, headers=headers, json=payload, timeout=60)
            if res.status_code == 200:
                break
            time.sleep(2 * (retry_i + 1))
        except Exception as e:
            print(f"[warning] Groq request error: {e}", flush=True)
            time.sleep(2)

    if res is None or res.status_code != 200:
        err_msg = res.text[:200] if res is not None else "No response"
        raise RuntimeError(f"Groq API call failed: {err_msg}")

    result_json = res.json()
    raw_text = result_json["choices"][0]["message"]["content"]
    meta = json.loads(raw_text)

    # Mandatory topic check and retry
    topic = meta.get("topic") or meta.get("inferred_topic") or ""
    topic = str(topic).strip()
    if not topic:
        print("[meta] Missing topic. Retrying Groq call with explicit topic reminder...", flush=True)
        retry_prompt = prompt + "\n\nREMINDER: You forgot 'topic' previously. You MUST provide the 'topic' key in the JSON output!"
        payload["messages"][0]["content"][0]["text"] = retry_prompt
        try:
            r_retry = requests.post(url, headers=headers, json=payload, timeout=60)
            if r_retry.status_code == 200:
                retry_json = r_retry.json()
                meta_retry = json.loads(retry_json["choices"][0]["message"]["content"])
                topic = meta_retry.get("topic") or meta_retry.get("inferred_topic") or ""
                topic = str(topic).strip()
                if topic:
                    meta = meta_retry
        except Exception as e:
            print(f"[meta] Retry error: {e}", flush=True)

    if not topic:
        topic = "Lifestyle & Inspiration"
        print("[meta] topic fallback used", flush=True)

    meta = sanitize_metadata(meta, duration, channel_name="Maureen Farr")
    meta["topic"] = topic
    meta["path"] = "video_ai_groq"
    meta["model_used"] = selected_model
    return meta

def generate_video_metadata_gemini(file_path, duration, api_key, model="gemini-flash-lite-latest"):
    prompt = (
        "Watch this video carefully and generate YouTube metadata in strict JSON format.\n"
        "Required JSON keys:\n"
        "- title: engaging YouTube title, strictly under 60 characters, no currency, no magnitude/number claims.\n"
        "- description: 2-3 engaging sentences describing what is happening in the video.\n"
        "- tags: array of 10 to 15 relevant keyword tags (single words or short phrases).\n"
        "- hashtags: array of exactly 3 relevant hashtags starting with '#' (include #shorts if short/vertical).\n"
        "- topic: a specific 1 to 4 word thematic niche/topic of this video.\n"
    )
    # Simple placeholder if called
    meta = {
        "title": "Inspiring Short Moment",
        "description": "An engaging moment captured on video.",
        "tags": ["shorts", "lifestyle", "daily", "video", "moments", "viral", "creative", "explore", "focus", "mindset"],
        "hashtags": ["#shorts", "#lifestyle", "#viral"],
        "topic": "Lifestyle & Inspiration"
    }
    meta = sanitize_metadata(meta, duration, channel_name="Maureen Farr")
    meta["path"] = "video_ai_gemini"
    meta["model_used"] = model
    return meta

def generate_video_metadata(file_path, duration, api_key, model="qwen/qwen3.8-27b", provider="groq"):
    if provider == "groq" or (model and "qwen" in model.lower()):
        return generate_video_metadata_groq(file_path, duration, api_key, model=model)
    else:
        return generate_video_metadata_gemini(file_path, duration, api_key, model=model)
