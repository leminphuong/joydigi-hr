from joydigi.settings import TEMPLATES

TEMPLATES[0]["OPTIONS"]["context_processors"].append(
    "joydigi_crumbs.context_processors.breadcrumbs",
)
