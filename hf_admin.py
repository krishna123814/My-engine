"""
hf_admin.py — Hugging Face Space ko control karne wale helpers
(restart / pause / PROXY_ON variable set karna).

Space ke Settings → Variables and secrets mein "HF_TOKEN" (write-permission
wala fine-grained token) save hona chahiye. SPACE_ID env var HF Spaces khud
provide karta hai (format: "username/space-name") — manually daalne ki
zaroorat nahi.
"""
from config import HF_TOKEN, HF_SPACE_ID


def restart_hf_space() -> tuple[bool, str]:
    """HF Space ko poora restart karta hai (naya process — saare purane
    background threads/WebSocket connections khatam ho jaate hain, proxy
    state fresh se load hota hai). Returns (ok, message)."""
    if not HF_TOKEN:
        return False, "HF_TOKEN secret nahi mila — Space Settings → Variables and secrets mein add karo."
    if not HF_SPACE_ID:
        return False, "SPACE_ID env var nahi mila — ye sirf Hugging Face Spaces par hi automatically available hota hai."
    try:
        from huggingface_hub import HfApi
        api = HfApi(token=HF_TOKEN)
        api.restart_space(repo_id=HF_SPACE_ID)
        return True, f"Restart trigger ho gaya — {HF_SPACE_ID} kuch second mein reload hogi."
    except Exception as e:
        return False, f"Restart fail: {e}"


def set_hf_proxy_variable(on: bool) -> tuple[bool, str]:
    """HF Space ke 'PROXY_ON' Variable (secret nahi) ko true/false set karta hai
    HF API se — ye HF ke metadata mein persist hota hai, ephemeral disk mein
    nahi, isliye restart/rebuild/sleep ke baad bhi wahi value load hoti hai.
    Variable change karte hi HF khud Space ko rebuild/restart kar deta hai
    (purani background threads/websockets is rebuild mein khatam ho jaati hain)."""
    if not HF_TOKEN:
        return False, "HF_TOKEN secret nahi mila — Space Settings → Variables and secrets mein add karo."
    if not HF_SPACE_ID:
        return False, "SPACE_ID env var nahi mila — ye sirf Hugging Face Spaces par hi automatically available hota hai."
    try:
        from huggingface_hub import HfApi
        api = HfApi(token=HF_TOKEN)
        api.add_space_variable(repo_id=HF_SPACE_ID, key="PROXY_ON", value="true" if on else "false")
        return True, f"PROXY_ON variable = {'true' if on else 'false'} set ho gaya — Space thodi der mein rebuild hogi."
    except Exception as e:
        return False, f"PROXY_ON variable set fail: {e}"


def pause_hf_space() -> tuple[bool, str]:
    """HF Space ko PAUSE karta hai — poori tarah band, saare background
    threads/proxy connections turant khatam. Jab tak khud restart_hf_space()
    ya HF dashboard se Resume na kiya jaaye, tab tak wapas nahi uthegi (48hr
    auto-sleep se alag — ye turant aur manual hai)."""
    if not HF_TOKEN:
        return False, "HF_TOKEN secret nahi mila — Space Settings → Variables and secrets mein add karo."
    if not HF_SPACE_ID:
        return False, "SPACE_ID env var nahi mila — ye sirf Hugging Face Spaces par hi automatically available hota hai."
    try:
        from huggingface_hub import HfApi
        api = HfApi(token=HF_TOKEN)
        api.pause_space(repo_id=HF_SPACE_ID)
        return True, f"Pause trigger ho gaya — {HF_SPACE_ID} ab band ho rahi hai. Wapas chalane ke liye HF dashboard se Resume/Restart karna hoga."
    except Exception as e:
        return False, f"Pause fail: {e}"
