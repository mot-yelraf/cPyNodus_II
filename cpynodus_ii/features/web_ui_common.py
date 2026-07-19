# ruff: noqa: E501
"""Shared compact HTML helpers loaded only by NodusWeb page requests."""

_NODUS_FAVICON = "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA1MTIgNTEyIiByb2xlPSJpbWciID48ZGVmcz48bGluZWFyR3JhZGllbnQgaWQ9Im5vZHVzLWdyYWRpZW50IiB4MT0iMTk0IiB5MT0iMTEyIiB4Mj0iMzcxIiB5Mj0iMzgyIiBncmFkaWVudFVuaXRzPSJ1c2VyU3BhY2VPblVzZSI+PHN0b3Agb2Zmc2V0PSIwIiBzdG9wLWNvbG9yPSIjMDhhZWVmIi8+PHN0b3Agb2Zmc2V0PSIwLjUyIiBzdG9wLWNvbG9yPSIjMjRjN2JmIi8+PHN0b3Agb2Zmc2V0PSIxIiBzdG9wLWNvbG9yPSIjN2RkYjEyIi8+PC9saW5lYXJHcmFkaWVudD48L2RlZnM+PHJlY3Qgd2lkdGg9IjUxMiIgaGVpZ2h0PSI1MTIiIHJ4PSI3MiIgZmlsbD0iI2ZmZiIvPjxwYXRoIGQ9Ik0xNDYgNDA3IEwxNDYgMzQ1IEwxMjQgMzMzIEwxODAgMzA2IEwxMTAgMjc5IEwxNzggMjUxIEwxNDYgMjM0IEwxNDYgMTMzIFExNDYgMTA2IDE3MiAxMDYgUTE4NiAxMDYgMTk3IDEyMiIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjMDhhZWVmIiBzdHJva2Utd2lkdGg9IjQwIiBzdHJva2UtbGluZWNhcD0icm91bmQiIHN0cm9rZS1saW5lam9pbj0icm91bmQiIC8+PHBhdGggZD0iTTE5NyAxMjIgTDM3MSAzODIiIGZpbGw9Im5vbmUiIHN0cm9rZT0idXJsKCNub2R1cy1ncmFkaWVudCkiIHN0cm9rZS13aWR0aD0iNDMiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIgc3Ryb2tlLWxpbmVqb2luPSJyb3VuZCIgLz48Y2lyY2xlIGN4PSIzOTMiIGN5PSIzODIiIHI9IjQyIiBmaWxsPSIjN2RkYjEyIi8+PGNpcmNsZSBjeD0iMzkzIiBjeT0iMzgyIiByPSIxNyIgZmlsbD0iI2ZmZiIvPjxwYXRoIGQ9Ik0zOTMgMzQwIEwzOTMgMTU3IiBmaWxsPSJub25lIiBzdHJva2U9IiM3ZGRiMTIiIHN0cm9rZS13aWR0aD0iNDAiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIgLz48Y2lyY2xlIGN4PSIzOTMiIGN5PSIxMTUiIHI9IjQyIiBmaWxsPSIjN2RkYjEyIi8+PGNpcmNsZSBjeD0iMzkzIiBjeT0iMTE1IiByPSIxNyIgZmlsbD0iI2ZmZiIvPjwvc3ZnPg=="


def html_escape(value):
    """Escape one value for HTML text or attribute use."""
    text = str(value if value is not None else "")
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def navigation(*, switch_present=False, automations=False):
    """Return links to independently rendered NodusWeb pages."""
    links = [
        ("/", "Status"),
        ("/setup", "Setup"),
        ("/calibration", "Calibration"),
    ]
    if switch_present:
        links.append(("/switch-setup", "Switch Settings"))
    if automations:
        links.append(("/automations-ui", "Automations"))
    links.append(("/info", "Nodus Info"))
    return "".join(
        '<a href="{}">{}</a>'.format(path, label) for path, label in links
    )


def render_page(hostname, title, body, *, nav="", script=""):
    """Wrap one compact independently loaded page."""
    script_block = "<script>{}</script>".format(script) if script else ""
    return """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Nodus {hostname}</title><link rel="icon" href="data:image/svg+xml;base64,{favicon}" type="image/svg+xml"><style>
*{{box-sizing:border-box}}body{{margin:0;background:#eef8f3;color:#1b1f24;font-family:Helvetica,Arial,sans-serif}}main{{max-width:760px;margin:auto;padding:14px}}header,.card{{background:#fff;border:1px solid #d4ded8;border-radius:10px;padding:14px;margin-bottom:12px}}header h1{{font-size:20px;margin:0 0 10px}}nav{{display:flex;gap:7px;flex-wrap:wrap}}nav a,button{{padding:8px 11px;border:0;border-radius:7px;background:#2d5bea;color:#fff;font-weight:700;text-decoration:none}}button.secondary{{background:#fff;color:#1b1f24;border:1px solid #cfd8e3}}button.danger{{background:#e4344f}}button.on{{background:#20864b}}button.off{{background:#1b1f24}}button:disabled{{opacity:1}}h2{{font-size:19px;margin:0 0 12px}}table{{width:100%;border-collapse:collapse}}th,td{{padding:8px;border-bottom:1px solid #edf1f5;text-align:left}}.row{{display:grid;grid-template-columns:165px 1fr;gap:8px;align-items:center;margin-bottom:9px}}.actions{{display:flex;justify-content:space-between;gap:9px;align-items:center;margin-top:12px}}input,select,textarea{{width:100%;min-width:0;padding:8px;border:1px solid #cfd8e3;border-radius:7px;font-size:15px;background:#fff}}textarea{{font:12px monospace}}.status,.hint,small{{font-size:12px;color:#586574}}.owner{{display:block;color:#20864b;margin-top:4px}}.owner:empty{{display:none}}details{{border:1px solid #dfe6ea;border-radius:8px;margin-bottom:10px}}summary{{cursor:pointer;font-weight:700;padding:10px}}.group{{padding:10px}}@media(max-width:600px){{.row{{grid-template-columns:1fr}}}}
</style>{script_block}</head><body><main><header><h1>{hostname}</h1><nav>{nav}</nav></header><section class="card"><h2>{title}</h2>{body}</section></main></body></html>""".format(
        hostname=html_escape(hostname),
        title=html_escape(title),
        nav=nav,
        body=body,
        script_block=script_block,
        favicon=_NODUS_FAVICON,
    )
