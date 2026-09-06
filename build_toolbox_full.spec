# -*- mode: python ; coding: utf-8 -*-
# B站工具箱 PyInstaller 打包配置（onedir，启动快、对 QtWebEngine 友好）
# 构建: pyinstaller build_toolbox.spec --noconfirm

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('tools/monitor/static', 'tools/monitor/static'),
        ('assets', 'assets'),
    ],
    hiddenimports=[
        'tools.comments.reply_pb2',
        'tools.comments.reply_pb2_grpc',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'PIL',
        'lxml',
        'grpc_tools',
    ],
    noarchive=False,
)

# ---- 体积裁剪：WebEngine 调试资源 / 多余语言包 / 无关 QML 模块 / 软件GL ----
import re as _re

_drop_bin = _re.compile(r'(opengl32sw|Qt6Quick3D|Qt63D)', _re.I)
a.binaries = [e for e in a.binaries if not _drop_bin.search(e[0])]


def _n(p):
    return p.replace("\\", "/")


a.datas = [e for e in a.datas if not (
    _re.search(r'devtools_resources|\.debug\.(pak|bin)$', _n(e[0]))
    or _re.search(r'PySide6/qml/Qt(3D|5Compat|Charts|DataVisualization|Graphs'
                  r'|Location|Multimedia|Positioning|RemoteObjects|Scxml'
                  r'|Sensors|Test|TextToSpeech|WebSockets|WebView|Quick3D)(/|$)',
                  _n(e[0]), _re.I)
    or ('qtwebengine_locales' in _n(e[0])
        and not _re.search(r'qtwebengine_locales/(zh-CN|en-US)\.pak$', _n(e[0])))
    or (_n(e[0]).startswith('PySide6/translations/qt')
        and '/qtwebengine_locales/' not in _n(e[0])
        and 'zh_CN' not in _n(e[0])))]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="B站工具箱(完整版)",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/icon.ico',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="B站工具箱(完整版)",
)
