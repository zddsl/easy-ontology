"""easy_ontology 配置装载：config.yaml + 平台适配。

容器内（正式形态）与 Windows 本机应急（run.bat）共用本模块：
launcher 按 sys.platform 选择 *_linux / *_windows。
"""
from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class BackendCfg(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000


class OntopCfg(BaseModel):
    cli_dir: str
    launcher_linux: str = "bin/ontop"
    launcher_windows: str = "ontop17.bat"
    port: int = 8083
    boot_timeout_s: int = 60

    @property
    def launcher(self) -> str:
        return self.launcher_windows if sys.platform == "win32" else self.launcher_linux

    @property
    def launcher_path(self) -> Path:
        return Path(self.cli_dir) / self.launcher


class SparqlCfg(BaseModel):
    timeout_s: int = 60
    max_rows: int = 1000


class RdfStoreCfg(BaseModel):
    """物化路线的 RDF 存储后端（QLever，SPARQL 协议标准端点）。"""
    base_url: str = "http://host.docker.internal:7001"
    kind: str = "qlever"
    enabled: bool = True

    @property
    def sparql_url(self) -> str:
        return self.base_url.rstrip("/") + "/sparql"


class DbTypeCfg(BaseModel):
    driver: str
    url_template: str
    url_params: str = ""
    ping_classpath: str | None = None
    hint: str = ""


class PathsCfg(BaseModel):
    data_dir: str = "/app/data"


class ToolsCfg(BaseModel):
    java_bin: str = "java"
    javac_bin: str = "javac"
    dbping_classpath_default: str = ""


class AppConfig(BaseModel):
    backend: BackendCfg = Field(default_factory=BackendCfg)
    ontop: OntopCfg
    sparql: SparqlCfg = Field(default_factory=SparqlCfg)
    rdf_store: RdfStoreCfg = Field(default_factory=RdfStoreCfg)
    db_types: dict[str, DbTypeCfg]
    paths: PathsCfg = Field(default_factory=PathsCfg)
    tools: ToolsCfg = Field(default_factory=ToolsCfg)
    auto_start_last_project: bool = True


def _config_path() -> Path:
    env = os.environ.get("EASY_ONTOLOGY_CONFIG")
    if env:
        return Path(env)
    # backend/config.py -> 项目根/config.yaml
    return Path(__file__).resolve().parent.parent / "config.yaml"


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    with open(_config_path(), encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return AppConfig(**raw)
