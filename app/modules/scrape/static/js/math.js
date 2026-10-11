// Typesets the TeX that arXiv and OpenAlex leave in titles and abstracts.
//
// Only elements marked `data-math` are touched -- the abstract and title
// containers -- never the whole page, so a dollar sign in a note, a search box
// or a flash message stays a dollar sign. Runs once when the page has parsed
// and again for every fragment HTMX swaps in (the feeds load that way).
//
// The stored text is not changed and neither is the server's HTML: if KaTeX
// never loads, the reader sees the TeX source, as before. KaTeX's default
// output carries MathML beside the visual HTML, which is what a screen reader
// reads.
//
// Delimiters: $$...$$, \[...\], \(...\) go straight to auto-render. Single
// dollars do not, because abstracts also talk about money: "costs $5 and $10"
// under a plain $...$ rule typesets "5 and " as math. Single dollars are
// instead taken under Pandoc's rule -- the opening $ must be followed by a
// non-space, the closing $ preceded by a non-space and not followed by a
// digit -- and every pair that passes is rewritten to \(...\) in the text node
// before auto-render runs. "$3.58\%$" and "$E_{\mathrm{inc}}$" pass; "$5 and
// $10" does not, since the would-be closer has a space before it. \$ is never
// a delimiter.
(function () {
  'use strict';

  var done = new WeakSet();

  function isSpace(ch) {
    return ch !== undefined && /\s/.test(ch);
  }

  function isDigit(ch) {
    return ch !== undefined && /[0-9]/.test(ch);
  }

  // Index of the $ that closes a single-dollar span opened just before
  // `start`, or -1 when Pandoc's rule says this $ opens nothing.
  function findClose(text, start) {
    if (start >= text.length || isSpace(text[start]) || text[start] === '$') {
      return -1;
    }
    for (var j = start; j < text.length; j++) {
      var ch = text[j];
      if (ch === '\\') {
        j++; // an escaped character, \$ included, closes nothing
        continue;
      }
      if (ch === '$') {
        if (isSpace(text[j - 1]) || isDigit(text[j + 1])) {
          return -1;
        }
        return j;
      }
    }
    return -1;
  }

  // Rewrites every Pandoc-valid $...$ in `text` to \(...\); everything else,
  // $$...$$ included, is copied through for auto-render to handle.
  function rewriteDollars(text) {
    if (text.indexOf('$') === -1) {
      return text;
    }
    var out = '';
    var i = 0;
    while (i < text.length) {
      var ch = text[i];
      if (ch === '\\' && i + 1 < text.length) {
        out += ch + text[i + 1];
        i += 2;
        continue;
      }
      if (ch === '$' && text[i + 1] === '$') {
        var end = text.indexOf('$$', i + 2);
        var stop = end === -1 ? text.length : end + 2;
        out += text.slice(i, stop);
        i = stop;
        continue;
      }
      if (ch === '$') {
        var close = findClose(text, i + 1);
        if (close !== -1) {
          out += '\\(' + text.slice(i + 1, close) + '\\)';
          i = close + 1;
          continue;
        }
      }
      out += ch;
      i++;
    }
    return out;
  }

  function typesetOne(el) {
    if (done.has(el)) {
      return;
    }
    done.add(el);
    var walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    var node;
    while ((node = walker.nextNode())) {
      var rewritten = rewriteDollars(node.nodeValue);
      if (rewritten !== node.nodeValue) {
        node.nodeValue = rewritten;
      }
    }
    window.renderMathInElement(el, {
      delimiters: [
        { left: '$$', right: '$$', display: true },
        { left: '\\[', right: '\\]', display: true },
        { left: '\\(', right: '\\)', display: false },
      ],
      throwOnError: false,
      // TeX KaTeX cannot parse is shown as its source in the surrounding
      // text colour, not KaTeX's default red: a red that is not an error
      // state would break the danger-colour rule in docs/DESIGN.md.
      errorColor: 'currentColor',
    });
  }

  function typeset(root) {
    if (typeof window.renderMathInElement !== 'function' || !root || !root.querySelectorAll) {
      return; // KaTeX did not load: leave the source text exactly as it was
    }
    if (root.matches && root.matches('[data-math]')) {
      typesetOne(root);
    }
    root.querySelectorAll('[data-math]').forEach(typesetOne);
  }

  typeset(document.body);
  document.addEventListener('htmx:load', function (event) {
    typeset(event.target);
  });
})();
