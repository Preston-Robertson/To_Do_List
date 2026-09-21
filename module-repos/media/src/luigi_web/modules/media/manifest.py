from ...core.module_registry import Module, NavigationItem

module = Module(
    id="media", label="Media", description="Games, shows, and Game'N'Watch integrations.",
    router="luigi_web.modules.media.routes:router", package="luigi_web.modules.media",
    navigation=(
        NavigationItem("games", "Games", "/games", "gamepad-2", "Library"),
        NavigationItem("shows", "Shows", "/shows", "tv-minimal", "Library"),
    ),
)