import io
p = 'app.py'
s = io.open(p, encoding='utf-8').read()

s = s.replace("import config\nimport export_pdf", "import config\nimport dataio\nimport export_pdf", 1)

anchor = "@app.get(\"/api/transactions\")\ndef api_transactions():"
block = '''# --------------------------------------------------------------------------- 数据导出 / 导入

MAX_UPLOAD_BYTES = 256 * 1024 * 1024     # 整库快照可能不小，但也不能无上限


@app.get("/api/data/export.db")
def api_export_db():
    """整库快照。跑着也能导——走 sqlite3 在线备份，不是复制文件。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "snapshot.db")
        try:
            store.backup_to(path)
        except Exception as exc:
            log.exception("导出数据库失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500
        with open(path, "rb") as fh:
            blob = fh.read()
    return Response(
        blob,
        mimetype="application/x-sqlite3",
        headers={
            "Content-Disposition": f'attachment; filename="stockwatch-{stamp}.db"',
            "Content-Length": str(len(blob)),
        },
    )


@app.get("/api/data/export.json")
def api_export_json():
    """可移植数据：账户、流水、追踪列表、点评。不含行情缓存，也不含任何密钥。"""
    try:
        payload = dataio.export_payload()
    except Exception as exc:
        log.exception("导出 JSON 失败")
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500
    blob = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    return Response(
        blob,
        mimetype="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="stockwatch-{stamp}.json"',
            "Content-Length": str(len(blob)),
        },
    )


@app.get("/api/data/stats")
def api_data_stats():
    """导出前给前端报个数，让人知道自己要导的是多少东西。"""
    info = store.inspect_db(store.DB_PATH)
    size = os.path.getsize(store.DB_PATH) if os.path.exists(store.DB_PATH) else 0
    return jsonify({"ok": True, "size_bytes": size, **info})


@app.post("/api/data/import")
def api_import_data():
    """导入一个文件。按内容判断是整库快照还是 JSON，不看扩展名——
    扩展名是用户随手改的，文件头不会骗人。"""
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify({"ok": False, "error": "没有收到文件"}), 400

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "upload.bin")
        upload.save(path)
        size = os.path.getsize(path)
        if size == 0:
            return jsonify({"ok": False, "error": "文件是空的"}), 400
        if size > MAX_UPLOAD_BYTES:
            return jsonify({"ok": False, "error": f"文件太大（{size / 1e6:.0f} MB）"}), 400

        with open(path, "rb") as fh:
            head = fh.read(16)

        if head == b"SQLite format 3\x00":
            check = store.inspect_db(path)
            if not check["ok"]:
                return jsonify({"ok": False, "error": check["error"]}), 400
            try:
                backup = store.replace_with(path)
            except Exception as exc:
                log.exception("替换数据库失败")
                return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500
            _state["quotes"] = {}      # 旧库的行情缓存跟新库对不上，清掉重抓
            threading.Thread(target=poll_once, kwargs={"reason": "import"}, daemon=True).start()
            log.warning("数据库已被整体替换，替换前的备份：%s", backup)
            return jsonify({"ok": True, "mode": "db", "counts": check["counts"],
                            "backup": os.path.basename(backup)})

        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (ValueError, OSError, UnicodeDecodeError) as exc:
            return jsonify({"ok": False,
                            "error": f"既不是数据库文件也不是合法 JSON：{exc}"}), 400
        try:
            report = dataio.import_payload(payload)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:
            log.exception("导入 JSON 失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500
        return jsonify({"ok": True, "mode": "json", **report})


@app.get("/api/transactions")
def api_transactions():'''

assert anchor in s
s = s.replace(anchor, block, 1)

# 需要的标准库
s = s.replace("import logging\nimport os\nimport threading",
              "import json\nimport logging\nimport os\nimport tempfile\nimport threading", 1)
io.open(p, 'w', encoding='utf-8', newline='\n').write(s)
print("app.py 端点已加")
