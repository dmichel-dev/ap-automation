# Third-party material

The public demo pages (demo.davidmichel.dev) serve their own fonts and icons: nothing is loaded from another site. The licence texts are in [`services/core/app/web/static/licenses/`](services/core/app/web/static/licenses) and are served with the pages, under `/static/licenses/`. The demo's credits page (`/credits`, linked from every page's footer) lists the same material.

| Material | Version | Used for | Licence |
| --- | --- | --- | --- |
| [Bricolage Grotesque](https://github.com/ateliertriay/bricolage) (variable weight, Latin subset, WOFF2) | Fontsource 5.3.0 | Headings | [SIL Open Font License 1.1](services/core/app/web/static/licenses/OFL-bricolage-grotesque.txt) |
| [Inter](https://github.com/rsms/inter) (variable weight, Latin subset, WOFF2) | Fontsource 5.3.0 | Body text | [SIL Open Font License 1.1](services/core/app/web/static/licenses/OFL-inter.txt) |
| [JetBrains Mono](https://github.com/JetBrains/JetBrainsMono) (500, Latin subset, WOFF2) | Fontsource 5.3.0 | Small labels, codes | [SIL Open Font License 1.1](services/core/app/web/static/licenses/OFL-jetbrains-mono.txt) |
| [Tabler Icons](https://tabler.io/icons), outline: inbox, scan, checklist, arrows-split-2, external-link, arrow-left | 3.53.0 | Decorative icons, copied as SVG paths into [`demo_icons.html`](services/core/app/web/templates/demo_icons.html) | [MIT](services/core/app/web/static/licenses/MIT-tabler-icons.txt) |

The fonts are in [`services/core/app/web/static/fonts/`](services/core/app/web/static/fonts). Text in other scripts (Arabic, Devanagari, CJK) uses the visitor's system fonts. To add an icon, copy its paths from the same Tabler version into `demo_icons.html` and list it above and on the credits page ([`demo_credits.html`](services/core/app/web/templates/demo_credits.html)).
