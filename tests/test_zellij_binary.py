"""The zellij choice: the web build while watching is on, else the usual one."""

import os

import pytest

from tongs_agents import paths
from tongs_agents.commands import web
from tongs_agents.multiplexers import zellij_binary

PATH_ZELLIJ = "/usr/bin/zellij"


def _install(link, directory_name):
    directory = paths.state_dir(create=True) / "bin" / directory_name
    directory.mkdir(parents=True, mode=0o700)
    binary = directory / "zellij"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o700)
    os.symlink(os.path.join(directory_name, "zellij"), link)
    return str(link)


@pytest.fixture
def managed():
    return _install(zellij_binary.managed_link(), "zellij-0.45.1")


@pytest.fixture
def web_build():
    return _install(zellij_binary.managed_web_link(), "zellij-web-0.45.1")


@pytest.fixture
def path_version(monkeypatch):
    version = {"value": (0, 44, 2)}
    monkeypatch.setattr(zellij_binary.shutil, "which", lambda name: PATH_ZELLIJ)
    monkeypatch.setattr(zellij_binary, "_cached_version", lambda binary: version["value"])
    return version


def test_usual_binary_prefers_path_at_the_floor(managed, path_version):
    assert zellij_binary.usual_binary() == PATH_ZELLIJ


def test_usual_binary_falls_back_to_managed_below_the_floor(managed, path_version):
    path_version["value"] = (0, 44, 0)

    assert zellij_binary.usual_binary() == managed


def test_usual_binary_keeps_path_without_a_managed_build(path_version):
    path_version["value"] = (0, 44, 0)

    assert zellij_binary.usual_binary() == PATH_ZELLIJ


def test_usual_binary_ignores_web_watching(managed, web_build, path_version):
    web.write_state(True, 8083)

    assert zellij_binary.usual_binary() == PATH_ZELLIJ
    assert zellij_binary.zellij_binary() == web_build


def test_zellij_binary_is_the_usual_one_while_watching_is_off(web_build, path_version):
    web.write_state(False, 8083)

    assert zellij_binary.zellij_binary() == zellij_binary.usual_binary() == PATH_ZELLIJ
