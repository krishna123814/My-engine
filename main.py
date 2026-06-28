import streamlit as st
import streamlit.components.v1 as components
import os
import json

st.set_page_config(
    page_title="BankNifty Live Chart",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)
st.markdown("""<style>
#MainMenu                        {display:none!important}
footer                           {display:none!important}
header                           {display:none!important}
[data-testid="stToolbar"]        {display:none!important}
[data-testid="stDecoration"]     {display:none!important}
[data-testid="stStatusWidget"]   {display:none!important}
[data-testid="manage-app-button"]{display:none!important}
.reportview-container .main footer{display:none!important}
.viewerBadge_container__1QSob   {display:none!important}
.styles_viewerBadge__1yB5_      {display:none!important}
#stDecoration                    {display:none!important}
.main .block-container{padding:0!important;max-width:100%!important;margin:0!important}
.stApp{background:#131722;overflow:hidden}
iframe{border:none!important}
</style>""", unsafe_allow_html=True)

CHART_STATE_FILE = "chart_state.json"

def _build_chart_html() -> str:
    _html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chart.html")
    if not os.path.exists(_html_path):
        return "<p style='color:red'>chart.html not found</p>"
    with open(_html_path, "r", encoding="utf-8") as f:
        html = f.read()

    # Saved chart-state restore
    _cs_state_raw = None
    try:
        if os.path.exists(CHART_STATE_FILE):
            with open(CHART_STATE_FILE, "r", encoding="utf-8") as csf:
                _cs_state_raw = csf.read().strip()
            json.loads(_cs_state_raw)
    except Exception:
        _cs_state_raw = None
    if _cs_state_raw:
        _cs_safe = _cs_state_raw.replace("</", "<\\/")
        html = html.replace("</body>", f"\n<script>window.__CHART_STATE_RESTORE__ = {_cs_safe};</script>\n</body>")

    return html

components.html(_build_chart_html(), height=950, scrolling=False)
