# SplashSplat project page

Single-page project website for **SplashSplat: Reconstructing Splashing Liquids from
Real-World Multi-View Videos**, served by GitHub Pages at
<https://niko-creater.github.io/splashsplat-web/>. Plain HTML, CSS and JavaScript: no
framework, no build step.

```
splashsplat-web/
├── index.html                 # the page (all text lives here)
├── .nojekyll                  # serve the files as-is
├── static/
│   ├── css/style.css
│   ├── js/main.js             # lazy video loading, tabs, BibTeX copy
│   ├── images/                # teaser, pipeline figure, posters, thumbnails, favicons
│   └── videos/                # H.264 MP4 clips + manifest.json
└── tools/build_assets.py      # rebuilds static/images and static/videos from the source media
```

Edit `index.html` to change the text. Run `tools/build_assets.py` (in the `fluid-splat`
environment) to regenerate the media from the research repo. Pushing to `main` redeploys
the site.
