// "The web", for the e2e suite: one article and a permissive robots.txt on
// localhost. The server and worker cannot reach anything else (scripts/e2e.py
// points their proxy at a closed port), so a spec that adds a link adds this.
import { createServer } from "node:http";

export const ARTICLE_TITLE = "Perovskite tandem cells under field conditions (e2e fixture)";

const PAGES = {
  "/robots.txt": ["text/plain", "User-agent: *\nAllow: /\n"],
  "/articles/perovskite.html": [
    "text/html; charset=utf-8",
    `<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>${ARTICLE_TITLE}</title></head>
<body>
  <article>
    <h1>${ARTICLE_TITLE}</h1>
    <p>Perovskite-silicon tandem modules were measured outdoors for a full
    year. This page is a fixture for the ScrapeMind end-to-end suite and
    contains no real findings; it is long enough that a reader extracting the
    main text has a paragraph to keep rather than only a heading.</p>
    <p>Degradation was tracked against temperature and irradiance, and the
    encapsulation mattered more than the cell stack.</p>
  </article>
</body>
</html>`,
  ],
};

const port = Number(process.env.E2E_STUB_PORT);

createServer((req, res) => {
  const page = PAGES[new URL(req.url, "http://localhost").pathname];
  if (!page) {
    res.writeHead(404).end();
    return;
  }
  res.writeHead(200, { "content-type": page[0] }).end(page[1]);
}).listen(port, "127.0.0.1");
