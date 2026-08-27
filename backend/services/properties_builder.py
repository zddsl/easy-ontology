"""数据源表单 → Ontop jdbc.properties（生成物，与验证过的 hd-mysql.properties 同形）。"""
from __future__ import annotations

from backend.config import get_config


def build_jdbc_url(form: dict) -> str:
    cfg = get_config().db_types[form["db_type"]]
    url = cfg.url_template.format(host=form["host"], port=form["port"], database=form.get("database", ""))
    if cfg.url_params:
        url += "?" + cfg.url_params
    return url


def build_properties_text(form: dict) -> str:
    cfg = get_config().db_types[form["db_type"]]
    return "\n".join([
        f"jdbc.url = {build_jdbc_url(form)}",
        f"jdbc.user = {form['user']}",
        f"jdbc.password = {form['password']}",
        f"jdbc.driver = {cfg.driver}",
        "",
    ])


def validate_form(form: dict) -> str | None:
    """返回错误文案；None 表示通过。"""
    cfg = get_config().db_types
    if form.get("db_type") not in cfg:
        return f"不支持的数据库类型：{form.get('db_type')}（可选：{'/'.join(cfg)}）"
    # dm8 的 url_template 不含 {database}，允许不填库名
    needs_db = "{database}" in cfg[form["db_type"]].url_template
    for key in ("host", "port", "user", "password"):
        if not form.get(key):
            return f"缺少字段：{key}"
    if needs_db and not form.get("database"):
        return "缺少字段：database"
    return None
