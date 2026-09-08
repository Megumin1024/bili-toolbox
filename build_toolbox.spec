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
        # 精简版：排除 QtWebEngine（内嵌 Chromium 约 +300MB）。监控页自动
        # 回退系统浏览器打开仪表盘；如需内嵌视图用 build_toolbox_full.spec
        'PySide6.QtWebEngineWidgets',
        'PySide6.QtWebEngineCore',
        'PySide6.QtWebChannel',
        'PySide6.QtQuick',
        'PySide6.QtQml',
        'PySide6.QtQuick3D',
        'PySide6.QtOpenGL',
        # 运行时用不到的连带依赖（openpyxl 可选加速库 / proto 编译器）
        'PIL',
        'lxml',
        'grpc_tools',
    ],
    noarchive=False,
)

# ---- 体积裁剪：纯 Widgets 应用不需要的 Qt 组件 ----
import re as _re

_DROP_BIN = _re.compile(r'(opengl32sw|Qt6(Qml|Quick|Quick3D|Pdf|Charts|3D))', _re.I)
a.binaries = [e for e in a.binaries if not _DROP_BIN.search(e[0])]
_DROP_DATA = _re.compile(r'PySide6/(qml/|translations/)')
a.datas = [e for e in a.datas
           if not (_DROP_DATA.search(e[0].replace("\\", "/"))
                   and 'zh_CN' not in e[0])]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='B站工具箱',
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
    name='B站工具箱',
)
