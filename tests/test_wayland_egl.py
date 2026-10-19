"""Scope the Qt EGL workaround without creating a GUI or native surface."""

import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from markdown_finder import app


_RESIZE_CHECK = "QT_WAYLAND_DISABLE_RESIZECHECK"


class WaylandEglConfigurationTests(unittest.TestCase):
    def configure(self, *, platform="linux", backend="wayland", version="6.11.2",
                  environment=None):
        application = SimpleNamespace(platformName=lambda: backend)
        with patch.dict(os.environ, environment or {}, clear=True), \
                patch.object(app.sys, "platform", platform), \
                patch.object(app, "qVersion", return_value=version):
            app._configure_wayland_egl_resize(application)
            return dict(os.environ)

    def test_affected_native_wayland_backends_enable_resize_refresh(self):
        for backend in ("wayland", "wayland-egl"):
            with self.subTest(backend=backend):
                environment = self.configure(backend=backend)
                self.assertEqual(environment[_RESIZE_CHECK], "1")

    def test_other_backends_do_not_inherit_the_workaround(self):
        for backend in ("xcb", "offscreen", "minimal", "windows", "cocoa"):
            with self.subTest(backend=backend):
                self.assertNotIn(_RESIZE_CHECK, self.configure(backend=backend))

    def test_other_operating_systems_do_not_enable_the_workaround(self):
        for platform in ("darwin", "win32", "freebsd14"):
            with self.subTest(platform=platform):
                self.assertNotIn(_RESIZE_CHECK, self.configure(platform=platform))

    def test_other_qt_versions_require_a_new_call_path_review(self):
        # New Qt versions can move resize handling into the per-frame path.
        for version in ("6.8.3", "6.10.2", "6.11.1", "6.11.3", "6.12.0", "7.0.0"):
            with self.subTest(version=version):
                self.assertNotIn(_RESIZE_CHECK, self.configure(version=version))

    def test_explicit_environment_override_is_preserved(self):
        # An explicit empty value also disables Qt's integer-valued option.
        for value in ("0", "1", ""):
            with self.subTest(value=value):
                environment = self.configure(environment={_RESIZE_CHECK: value})
                self.assertEqual(environment[_RESIZE_CHECK], value)

    def test_configuration_preserves_unrelated_environment(self):
        environment = self.configure(environment={"READER_TEST_SETTING": "unchanged"})
        self.assertEqual(environment, {
            "READER_TEST_SETTING": "unchanged", _RESIZE_CHECK: "1",
        })


if __name__ == "__main__":
    unittest.main()
