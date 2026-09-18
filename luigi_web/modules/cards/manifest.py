from ...core.module_registry import Module, NavigationItem

module = Module(
    id="cards", label="Trading Cards", description="Catalogs, decks, collections, and price history.",
    router="luigi_web.modules.cards.routes:router", package="luigi_web.modules.cards",
    template_setup="luigi_web.modules.cards.templating:register_filters",
    startup="luigi_web.modules.cards.lifecycle:start", shutdown="luigi_web.modules.cards.lifecycle:stop",
    navigation=(NavigationItem("cards", "Trading Cards", "/cards", "layers", "Library"),),
)