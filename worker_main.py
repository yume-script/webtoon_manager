# -*- coding: utf-8 -*-
"""
웹툰 다운로더 별도 작업 프로세스 시작점(worker_ctl.spawn()이 실행한다).

BookOasis 없이 같은 플러그인 코드를 불러오기 위해 plugins / plugins.metadata 패키지와
BaseMetadataProvider만 가벼운 대역으로 끼워 넣는다. 설정은 플러그인이 넘겨준
worker_cfg.json에서 읽는다(BookOasis DB는 쓰지 않음).
"""
import importlib
import json
import os
import sys
import types


def _install_stubs(plugin_dir):
    metadata_dir = os.path.dirname(plugin_dir)
    plugins_dir = os.path.dirname(metadata_dir)
    data_dir = os.path.join(plugins_dir, "data", os.path.basename(plugin_dir))
    cfg_path = os.path.join(data_dir, "worker_cfg.json")

    pk = types.ModuleType("plugins")
    pk.__path__ = [plugins_dir]
    md = types.ModuleType("plugins.metadata")
    md.__path__ = [metadata_dir]
    base = types.ModuleType("plugins.metadata.base")

    class BaseMetadataProvider(object):
        def __init__(self, *a, **k):
            pass

        def get_plugin_config(self, db_type=None, default=None):
            try:
                with open(cfg_path, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:  # noqa: BLE001
                return default if default is not None else {}

        def set_plugin_config(self, db_type, cfg):
            # 작업 프로세스는 설정을 BookOasis에 저장하지 않는다(설정 저장은 화면 쪽에서만)
            raise RuntimeError("작업 프로세스에서는 설정을 저장하지 않음")

        def get_db_gateway(self, db_type=None):
            raise RuntimeError("작업 프로세스에서는 BookOasis DB를 쓰지 않음")

    base.BaseMetadataProvider = BaseMetadataProvider
    sys.modules["plugins"] = pk
    sys.modules["plugins.metadata"] = md
    sys.modules["plugins.metadata.base"] = base
    return "plugins.metadata." + os.path.basename(plugin_dir)


def main():
    plugin_dir = os.path.dirname(os.path.abspath(__file__))
    os.environ["WTM_WORKER"] = "1"
    pkg = _install_stubs(plugin_dir)
    ctl = importlib.import_module(pkg + ".worker_ctl")
    ctl.run_worker()


if __name__ == "__main__":
    main()
