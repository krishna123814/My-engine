import streamlit as st
import streamlit.components.v1 as components
from urllib.parse import quote_plus

st.set_page_config(page_title="Multi-Engine Mini Browser", page_icon="🔍", layout="wide")
st.title("🔍 Multi-Engine Mini Browser")
st.caption(
    "Search word daalo — neeche alag-alag search engines tabs mein try honge. "
    "Jo tab blank/error dikhaye, samajh lo usne iframe block kar diya."
)

query = st.text_input("Search kya karna hai?", value="", placeholder="e.g. python tutorial")

# List of search engines to try. {q} gets replaced with the encoded search query.
ENGINES = [
    {"name": "Google", "url": "https://www.google.com/search?q={q}"},
    {"name": "Bing", "url": "https://www.bing.com/search?q={q}"},
    {"name": "DuckDuckGo (HTML)", "url": "https://html.duckduckgo.com/html/?q={q}"},
    {"name": "DuckDuckGo (Lite)", "url": "https://lite.duckduckgo.com/lite/?q={q}"},
    {"name": "Startpage", "url": "https://www.startpage.com/sp/search?query={q}"},
    {"name": "Brave Search", "url": "https://search.brave.com/search?q={q}"},
    {"name": "Yahoo", "url": "https://search.yahoo.com/search?p={q}"},
    {"name": "SearXNG (public instance)", "url": "https://searx.be/search?q={q}"},
]

if query.strip():
    encoded_q = quote_plus(query.strip())

    try:
        with open("chart.html", "r", encoding="utf-8") as f:
            html_template = f.read()
    except FileNotFoundError:
        st.error("chart.html file nahi mili. Ye file main.py ke sath same folder mein honi chahiye.")
        st.stop()

    tabs = st.tabs([e["name"] for e in ENGINES])

    for tab, engine in zip(tabs, ENGINES):
        with tab:
            full_url = engine["url"].format(q=encoded_q)
            st.caption(f"URL: `{full_url}`")

            html_content = html_template.replace("{{URL}}", full_url)
            components.html(html_content, height=600, scrolling=True)

            st.link_button(
                f"↗️ {engine['name']} ko naye tab mein kholo",
                full_url,
                use_container_width=True,
            )
else:
    st.info("Upar search box mein kuch type karo — phir sabhi engines ke results yahin tabs mein dikhenge.")
