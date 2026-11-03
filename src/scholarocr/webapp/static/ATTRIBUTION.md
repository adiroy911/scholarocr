# Third-party files in the web app

Everything here is redistributed under its own licence. The licence texts sit next to the files.

| Files | What | Source | Licence |
|---|---|---|---|
| `vendor/katex/katex.min.js`, `katex.min.css`, `fonts/*.woff2` | KaTeX 0.16.22, math rendering | https://katex.org, npm `katex@0.16.22` | MIT, `vendor/katex/LICENSE` |
| `vendor/katex/mhchem.min.js` | KaTeX mhchem extension (`\ce{}` chemistry), from the same KaTeX release | npm `katex@0.16.22/dist/contrib` | MIT (KaTeX port) and Apache-2.0 (original mhchem: MathJax Consortium, Martin Hensel), `vendor/katex/LICENSE-mhchem.txt` |
| `vendor/fonts/anek-*.woff2` | Anek Latin and Anek Devanagari (variable: weight and width), interface type | Ek Type, https://github.com/EkType/Anek, via Fontsource `@fontsource-variable/anek-latin@5.3.0` and `anek-devanagari@5.3.0` | SIL OFL 1.1, `vendor/fonts/LICENSE-Anek.txt` |
| `vendor/fonts/source-serif-4-*.woff2` | Source Serif 4, text of extracted documents | Adobe (Frank Grießhammer), https://fonts.google.com/specimen/Source+Serif+4, via Fontsource | SIL OFL 1.1, `vendor/fonts/LICENSE-SourceSerif4.txt` |
| `vendor/fonts/noto-serif-devanagari-*.woff2` | Noto Serif Devanagari, Devanagari in extracted documents | The Noto Project Authors, https://github.com/notofonts/devanagari, via Fontsource | SIL OFL 1.1, `vendor/fonts/LICENSE-NotoSerifDevanagari.txt` |
| `icons.svg` (all symbols except `arrow-up`, `more`, `stop`) | Lucide icons, combined into one sprite | Lucide Contributors (portions Cole Bemis, Feather, MIT), https://lucide.dev, `lucide-static@0.460.0` | ISC, `LICENSE-lucide.txt` |

Drawn for ScholarOCR (no third-party rights): `mark.svg`, `mark-dark.svg`, and the `arrow-up`, `more` and `stop`
symbols in `icons.svg`.

The serif fonts and the icons come from a vetted asset set whose licence manifest was checked for redistribution in
an MIT-licensed repository.
