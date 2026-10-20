# Third-party notices

forge-ui's 3D view is ported from archivision and draws in the iso-glow style. The served
copies of that code (`forge_ui/static/iso.js`, `scene3d.js` and `runmodel.js`) carry these
notices in their head comments; this file keeps them with the source.

## archivision

`forge_ui/static/iso.js` and `scene3d.js` port `assets/shell-3d.html` and `assets/figure.js`;
`forge_ui/static/runmodel.js` ports `lib/layout.js` (https://github.com/Giamme/archivision).

```
MIT License

Copyright (c) 2026 Giamme

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## iso-glow and iso-figure

archivision's shell and the drawing style of its figures come from iso-glow, which is based
on iso-figure. The same code is in `forge_ui/static/iso.js` and `scene3d.js`.

```
MIT License

Copyright (c) 2026 April Zhu (iso-glow: lighting, palettes, gallery shell, examples)
Copyright (c) 2026 Tolga Cohce (iso-figure, on which iso-glow is based)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## three.js

three.js 0.160.0 (MIT, Copyright © 2010-2023 three.js authors) is not part of forge. The page
loads it, with its `examples/jsm` add-ons, from `cdn.jsdelivr.net` when the 3D view is used.
