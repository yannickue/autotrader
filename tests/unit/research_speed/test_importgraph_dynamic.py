# ruff: noqa: E501
"""Dynamic-import detection: every way of loading code the static closure cannot see must be flagged."""

from __future__ import annotations

import pytest

from research_speed.importgraph import dynamic_import_files

FLAGGED = {
    "plain_import_module": "import importlib\nm = importlib.import_module('x')\n",
    "module_alias": "import importlib as il\nm = il.import_module('x')\n",
    "from_alias": "from importlib import import_module as load\nm = load('x')\n",
    "from_plain": "from importlib import import_module\nm = import_module('x')\n",
    "dunder_import": "m = __import__('x')\n",
    "dunder_import_alias": "f = __import__\nm = f('x')\n",
    "builtins_dunder": "import builtins\nm = builtins.__import__('x')\n",
    "builtins_alias": "import builtins as b\nm = getattr(b, name)('x')\n",
    "getattr_literal": "import importlib\nm = getattr(importlib, 'import_module')('x')\n",
    "getattr_nonliteral": "import importlib\nm = getattr(importlib, name)('x')\n",
    "getattr_literal_any_object": "m = getattr(thing, 'import_module')('x')\n",
    "globals_lookup": "m = globals()['__import__']('x')\n",
    "spec_from_file_location": "import importlib.util as u\ns = u.spec_from_file_location('x', 'y')\n",
    "module_from_spec": "from importlib.util import module_from_spec\nm = module_from_spec(s)\n",
    "util_submodule_alias": "from importlib import util\ns = util.find_spec('x')\n",
    "exec_module": "s.loader.exec_module(m)\n",
    "runpy_run_path": "import runpy\nr = runpy.run_path('x.py')\n",
    "runpy_alias": "from runpy import run_module as rm\nr = rm('x')\n",
    "pkgutil_loader": "import pkgutil\nl = pkgutil.get_loader('x')\n",
    "pkgutil_walk": "from pkgutil import walk_packages\nfor _ in walk_packages(p):\n    pass\n",
    "pkgutil_resolve": "import pkgutil\nm = pkgutil.resolve_name('a.b')\n",
    "imp_load_source": "import imp\nm = imp.load_source('x', 'y')\n",
    "exec_import_string": "exec('import os')\n",
    "exec_nonliteral": "exec(code)\n",
    "eval_nonliteral": "m = eval(expr)\n",
    "eval_import_string": "m = eval(\"__import__('os')\")\n",
    "compile_nonliteral": "c = compile(src, 'f', 'exec')\n",
    "star_import_loader": "from importlib import *\nm = import_module('x')\n",
    "unparsable": "def broken(:\n",
}

NOT_FLAGGED = {
    "plain": "import os\nimport json\nx = os.path.join('a', 'b')\nd = getattr(os, 'sep')\n",
    "importlib_metadata_only": "from importlib import metadata\nv = metadata.version('numpy')\n",
    "literal_eval_without_import": "v = eval('1 + 2')\nc = compile('x = 1', 'f', 'exec')\n",
    "ast_literal_eval": "import ast\nv = ast.literal_eval('[1, 2]')\n",
    "getattr_plain_object": "class A:\n    x = 1\nv = getattr(A, 'x')\nw = getattr(A, name)\n",
    "word_in_docstring": '"""Uses import_module style loading in docs only."""\nx = 1\n',
}


@pytest.mark.parametrize("name", sorted(FLAGGED))
def test_dynamic_form_is_flagged(tmp_path, name) -> None:
    f = tmp_path / f"{name}.py"
    f.write_text(FLAGGED[name])
    assert dynamic_import_files([f]) == [f], name


@pytest.mark.parametrize("name", sorted(NOT_FLAGGED))
def test_plain_code_is_not_flagged(tmp_path, name) -> None:
    f = tmp_path / f"{name}.py"
    f.write_text(NOT_FLAGGED[name])
    assert dynamic_import_files([f]) == [], name


def test_mixed_batch_only_returns_the_dynamic_files(tmp_path) -> None:
    ok = tmp_path / "ok.py"
    ok.write_text(NOT_FLAGGED["plain"])
    bad = tmp_path / "bad.py"
    bad.write_text(FLAGGED["from_alias"])
    assert dynamic_import_files([ok, bad]) == [bad]
