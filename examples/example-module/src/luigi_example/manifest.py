"""Lightweight entry-point manifest; importing it does not load routes."""
from luigi_web.core.module_registry import Module, NavigationItem

module = Module(
    id="example",
    label="Example",
    description="Read-only module status",
    router="luigi_example.routes:router",
    package="luigi_example",
    api_version=1,
    navigation=(NavigationItem("example", "Example", "/extensions/example", "blocks"),),
)