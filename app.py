"""Flask API агента-диспетчера и одна HTML-страница.

Запуск: ``python app.py`` → http://127.0.0.1:5000
Роль передаётся заголовком X-Role (customer | manager) — учебный механизм вместо авторизации.
"""

from functools import wraps
from pathlib import Path

import anthropic
from flask import Flask, g, jsonify, request, send_from_directory

from dispatcher import audit, config, db, llm, views, workflow

STATIC_DIR = Path(__file__).resolve().parent / "static"
ROLES = ("customer", "manager")


def create_app(db_path: Path = config.DB_PATH, client_factory=llm.make_client) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["DB_PATH"] = Path(db_path)
    app.json.ensure_ascii = False

    with db.connect(app.config["DB_PATH"]) as conn:
        db.ensure_ready(conn)

    @app.before_request
    def open_db():
        g.conn = db.connect(app.config["DB_PATH"])

    @app.teardown_request
    def close_db(_error):
        conn = g.pop("conn", None)
        if conn is not None:
            conn.close()

    def role(*allowed: str):
        def wrap(view):
            @wraps(view)
            def checked(*args, **kwargs):
                current = request.headers.get("X-Role", "")
                if current not in ROLES:
                    return jsonify(error="Укажите роль в заголовке X-Role: customer или manager"), 401
                if current not in allowed:
                    audit.write(g.conn, kwargs.get("case_id"), current, "ACCESS_DENIED", {"path": request.path})
                    g.conn.commit()
                    return jsonify(error="Действие недоступно для этой роли"), 403
                return view(*args, **kwargs)
            return checked
        return wrap

    def body() -> dict:
        return request.get_json(silent=True) or {}

    @app.errorhandler(workflow.NotFound)
    def not_found(error):
        return jsonify(error=str(error)), 404

    @app.errorhandler(workflow.Conflict)
    def conflict(error):
        return jsonify(error=str(error)), 409

    @app.errorhandler(workflow.BadRequest)
    def bad_request(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(llm.LlmNotConfigured)
    def not_configured(error):
        return jsonify(error=str(error)), 503

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/api/cases")
    @role("manager")
    def list_cases():
        return jsonify(cases=views.queue(g.conn))

    @app.get("/api/cases/<case_id>")
    @role("manager", "customer")
    def show_case(case_id):
        if request.headers["X-Role"] == "manager":
            return jsonify(views.manager_case(g.conn, case_id))
        return jsonify(views.customer_case(g.conn, case_id))

    @app.post("/api/cases/<case_id>/analyze")
    @role("manager")
    def analyze(case_id):
        try:
            result = workflow.analyze(g.conn, case_id, client_factory())
        except anthropic.APIError as error:
            message = audit.redact(f"{type(error).__name__}: {error}")
            audit.write(g.conn, case_id, "server", "LLM_ERROR", {"error": message})
            g.conn.commit()
            return jsonify(error=f"Модель недоступна: {message}"), 502
        return jsonify(recommendation=result)

    @app.post("/api/cases/<case_id>/decision")
    @role("manager")
    def decide(case_id):
        data = body()
        outcome = workflow.decide(
            g.conn, case_id, data.get("decision", ""), data.get("comment", ""), data.get("customer_message", ""),
        )
        return jsonify(outcome)

    @app.post("/api/cases/<case_id>/reply")
    @role("customer")
    def reply(case_id):
        workflow.customer_reply(g.conn, case_id, body().get("text", ""))
        return jsonify(views.customer_case(g.conn, case_id))

    @app.post("/api/admin/reset")
    @role("manager")
    def reset():
        return jsonify(imported=db.reset(g.conn))

    return app


if __name__ == "__main__":
    create_app().run(debug=False)
