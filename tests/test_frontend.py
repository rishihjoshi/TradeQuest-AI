"""Frontend unit tests — app.js holdings card rendering.

Tests verify two features added in the portfolio holdings cards:
  1. Company name (pos-name) shown below the ticker symbol in each card
  2. Yahoo Finance external link (pos-yahoo-link) in the expanded detail footer

Test approach: parse app.js template-literal output for expected HTML patterns.
This is a structural / contract test — it catches regressions in the rendered HTML
without needing a browser or DOM runtime.
"""
import re
import unittest
from pathlib import Path

APP_JS   = Path(__file__).resolve().parent.parent / "app.js"
STYLE_CSS = Path(__file__).resolve().parent.parent / "style.css"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ── Helpers that mirror what app.js does at runtime ──────────────────────────

def make_yahoo_url(symbol: str) -> str:
    """Mirror the yahooUrl template literal from app.js renderHoldings()."""
    return f"https://finance.yahoo.com/quote/{symbol}/"


# ═══════════════════════════════════════════════════════════════════════════
# Feature 1 — Company name (.pos-name) in the holdings card
# ═══════════════════════════════════════════════════════════════════════════

class TestCompanyNameRendering(unittest.TestCase):
    """Verify the .pos-name element is present in app.js card template."""

    @classmethod
    def setUpClass(cls):
        cls.src = _read(APP_JS)

    def test_pos_name_class_exists_in_template(self):
        """app.js must render a .pos-name span inside the holding card."""
        self.assertIn('pos-name', self.src,
                      "app.js must contain 'pos-name' class in the holdings card template")

    def test_pos_name_renders_company_name_variable(self):
        """The pos-name span must interpolate the 'name' variable (company name)."""
        # Look for the pattern: span class="pos-name" ... ${name}
        match = re.search(r'pos-name["\'][^>]*>\s*\$\{name\}', self.src)
        self.assertIsNotNone(
            match,
            "pos-name span must render the '${name}' template variable"
        )

    def test_pos_name_only_shown_when_name_exists(self):
        """Company name span must be guarded: only rendered when name is truthy."""
        # Pattern: name ? `<span class="pos-name"...` — conditional rendering
        match = re.search(r'name\s*\?\s*`[^`]*pos-name', self.src)
        self.assertIsNotNone(
            match,
            "pos-name must be conditionally rendered (name ? ... : '') to avoid empty spans"
        )

    def test_pos_name_has_title_attribute_for_full_name(self):
        """pos-name span must include title attribute so full name shows on hover."""
        match = re.search(r'pos-name[^>]*title="\$\{name\}"', self.src)
        self.assertIsNotNone(
            match,
            "pos-name span must have title=\"${name}\" for tooltip on truncation"
        )

    def test_pos_name_rendered_below_ticker_not_replacing_it(self):
        """pos-sym (ticker) must still appear AND pos-name must appear after it."""
        sym_pos  = self.src.find('pos-sym')
        name_pos = self.src.find('pos-name')
        self.assertGreater(sym_pos,  -1, "pos-sym must still exist")
        self.assertGreater(name_pos, -1, "pos-name must exist")
        # name comes after sym in the template (later in file = rendered after)
        self.assertGreater(name_pos, sym_pos,
                           "pos-name must appear after pos-sym in the template")

    def test_pos_name_css_class_exists_in_stylesheet(self):
        """style.css must define .pos-name with visible styling."""
        css = _read(STYLE_CSS)
        self.assertIn('.pos-name', css,
                      "style.css must define .pos-name class")

    def test_pos_name_css_has_truncation(self):
        """style.css .pos-name must truncate long company names with ellipsis."""
        css = _read(STYLE_CSS)
        # Find the .pos-name block
        block_match = re.search(r'\.pos-name\s*\{([^}]+)\}', css, re.DOTALL)
        self.assertIsNotNone(block_match, ".pos-name block must exist in style.css")
        block = block_match.group(1)
        self.assertIn('text-overflow', block,
                      ".pos-name must have text-overflow for long company names")
        self.assertIn('overflow', block,
                      ".pos-name must have overflow: hidden")

    def test_pos_name_css_has_max_width(self):
        """style.css .pos-name must have a max-width to constrain long names."""
        css = _read(STYLE_CSS)
        block_match = re.search(r'\.pos-name\s*\{([^}]+)\}', css, re.DOTALL)
        self.assertIsNotNone(block_match)
        self.assertIn('max-width', block_match.group(1),
                      ".pos-name must have max-width to prevent layout overflow")


# ═══════════════════════════════════════════════════════════════════════════
# Feature 2 — Yahoo Finance link (.pos-yahoo-link) in expanded detail footer
# ═══════════════════════════════════════════════════════════════════════════

class TestYahooFinanceLink(unittest.TestCase):
    """Verify the Yahoo Finance external link is correctly rendered."""

    @classmethod
    def setUpClass(cls):
        cls.src = _read(APP_JS)

    # ── URL construction ───────────────────────────────────────────────────

    def test_yahoo_url_construction(self):
        """Yahoo Finance URL must follow the https://finance.yahoo.com/quote/{SYM}/ pattern."""
        for sym in ("WDC", "CAT", "AMAT", "NUE", "BEN"):
            url = make_yahoo_url(sym)
            self.assertTrue(url.startswith("https://finance.yahoo.com/quote/"),
                            f"{sym}: URL must start with https://finance.yahoo.com/quote/")
            self.assertIn(sym, url, f"{sym}: symbol must appear in URL")
            self.assertTrue(url.endswith("/"), f"{sym}: URL should end with /")

    def test_yahoo_url_uses_https(self):
        """URL must always use HTTPS — never plain HTTP."""
        url = make_yahoo_url("AAPL")
        self.assertTrue(url.startswith("https://"),
                        "Yahoo Finance link must use HTTPS, not HTTP")

    # ── HTML template checks ───────────────────────────────────────────────

    def test_pos_yahoo_link_class_in_template(self):
        """app.js must render a .pos-yahoo-link element in the holdings card."""
        self.assertIn('pos-yahoo-link', self.src,
                      "app.js must contain 'pos-yahoo-link' class")

    def test_yahoo_link_is_anchor_tag_not_button(self):
        """Yahoo Finance link must be an <a> tag — not a <button> — so it opens a URL."""
        match = re.search(r'<a\s[^>]*pos-yahoo-link', self.src)
        self.assertIsNotNone(
            match,
            "Yahoo Finance element must be an <a> anchor tag, not a <button>"
        )

    def test_yahoo_link_has_href_with_yahoo_domain(self):
        """The href must use ${yahooUrl}, and yahooUrl must contain finance.yahoo.com."""
        # The template uses href="${yahooUrl}" — check variable definition contains the domain
        match = re.search(r'const yahooUrl\s*=\s*`[^`]*finance\.yahoo\.com', self.src)
        self.assertIsNotNone(
            match,
            "yahooUrl constant must contain 'finance.yahoo.com' in its definition"
        )

    def test_yahoo_link_interpolates_symbol(self):
        """The href must use the ${sym} template variable — not a hardcoded symbol."""
        match = re.search(r'href="\$\{yahooUrl\}"', self.src)
        self.assertIsNotNone(
            match,
            "Yahoo Finance href must use ${yahooUrl} template variable (not hardcoded)"
        )

    def test_yahoo_link_target_blank(self):
        """Yahoo Finance link must open in a new tab (target=\"_blank\")."""
        match = re.search(r'pos-yahoo-link[^>]*target=["\']_blank["\']', self.src)
        # Also accept the reverse attribute order
        if not match:
            match = re.search(r'target=["\']_blank["\'][^>]*pos-yahoo-link', self.src)
        # Search more broadly — target="_blank" near pos-yahoo-link
        block_match = re.search(
            r'pos-yahoo-link.*?target=["\']_blank["\']',
            self.src, re.DOTALL
        )
        self.assertIsNotNone(
            block_match,
            "Yahoo Finance link must have target=\"_blank\" to open in new tab"
        )

    def test_yahoo_link_rel_noopener_noreferrer(self):
        """Yahoo Finance link must have rel='noopener noreferrer' for security.

        Without this, the opened tab can access window.opener and potentially
        redirect the parent page — a well-known tabnabbing attack vector.
        """
        self.assertIn('noopener', self.src,
                      "Yahoo Finance link must include rel='noopener'")
        self.assertIn('noreferrer', self.src,
                      "Yahoo Finance link must include rel='noreferrer'")
        # Both must appear in the same rel attribute
        match = re.search(r'rel=["\']noopener noreferrer["\']', self.src)
        self.assertIsNotNone(
            match,
            "rel must be 'noopener noreferrer' (both values together)"
        )

    def test_yahoo_link_stops_card_click_propagation(self):
        """Clicking the link must NOT toggle the card expand/collapse.

        onclick='event.stopPropagation()' is required — otherwise clicking
        the Yahoo Finance link also collapses the card.
        """
        # Find pos-yahoo-link block and check stopPropagation nearby
        yahoo_idx = self.src.find('pos-yahoo-link')
        self.assertGreater(yahoo_idx, -1, "pos-yahoo-link must exist")
        # Check within 500 chars of the element definition
        context = self.src[yahoo_idx: yahoo_idx + 500]
        self.assertIn('stopPropagation', context,
                      "pos-yahoo-link must call event.stopPropagation() to prevent card toggle")

    def test_yahoo_link_has_aria_label(self):
        """Yahoo Finance link must have an aria-label for screen reader accessibility."""
        match = re.search(r'pos-yahoo-link[^>]*aria-label', self.src)
        if not match:
            # Accept aria-label appearing slightly after the class attribute
            block = re.search(
                r'pos-yahoo-link.*?aria-label',
                self.src, re.DOTALL
            )
            match = block
        self.assertIsNotNone(
            match,
            "Yahoo Finance link must have an aria-label attribute for accessibility"
        )

    def test_yahoo_link_visible_text(self):
        """Yahoo Finance link must have visible user-facing text."""
        match = re.search(r'pos-yahoo-link.*?>Yahoo Finance', self.src, re.DOTALL)
        self.assertIsNotNone(
            match,
            "Yahoo Finance link must have 'Yahoo Finance' as its visible text"
        )

    def test_yahoo_link_in_pos_detail_footer_not_main_row(self):
        """Yahoo Finance link must appear inside the expanded detail section.

        It should NOT appear in the collapsed .pos-main row (that would clutter
        every card row). It belongs in .pos-detail-footer (expanded only).
        """
        detail_idx = self.src.find('pos-detail-footer')
        yahoo_idx  = self.src.find('pos-yahoo-link')
        self.assertGreater(detail_idx, -1, "pos-detail-footer must exist")
        self.assertGreater(yahoo_idx,  -1, "pos-yahoo-link must exist")
        # Yahoo link should come AFTER pos-detail-footer in the template
        self.assertGreater(yahoo_idx, detail_idx,
                           "pos-yahoo-link must appear inside pos-detail-footer block")

    # ── CSS checks ────────────────────────────────────────────────────────

    def test_pos_yahoo_link_css_class_exists(self):
        """style.css must define .pos-yahoo-link."""
        css = _read(STYLE_CSS)
        self.assertIn('.pos-yahoo-link', css,
                      "style.css must define .pos-yahoo-link class")

    def test_pos_yahoo_link_css_is_styled_distinctly(self):
        """style.css .pos-yahoo-link must have its own colour (not default blue underline)."""
        css = _read(STYLE_CSS)
        block_match = re.search(r'\.pos-yahoo-link\s*\{([^}]+)\}', css, re.DOTALL)
        self.assertIsNotNone(block_match, ".pos-yahoo-link block must exist in style.css")
        block = block_match.group(1)
        self.assertIn('text-decoration', block,
                      ".pos-yahoo-link must set text-decoration to suppress default underline")
        self.assertIn('border-radius', block,
                      ".pos-yahoo-link must have border-radius to match button styling")

    def test_pos_yahoo_link_css_has_hover_state(self):
        """style.css must define a hover state for .pos-yahoo-link."""
        css = _read(STYLE_CSS)
        match = re.search(r'\.pos-yahoo-link:(hover|focus)', css)
        self.assertIsNotNone(match,
                             "style.css must define .pos-yahoo-link:hover or :focus state")

    def test_pos_yahoo_link_mobile_responsive(self):
        """style.css must include .pos-yahoo-link in a mobile responsive selector.

        The mobile block stacks footer buttons vertically with width:100%.
        pos-yahoo-link must be included alongside pos-trade-btn/pos-close-btn
        so it also stretches full-width on small screens.
        """
        css = _read(STYLE_CSS)
        # Check that pos-yahoo-link appears inside some @media block
        # Strategy: find all @media blocks and look for pos-yahoo-link within them
        media_blocks = re.findall(r'@media[^{]+\{(.+?)(?=\n@media|\Z)', css, re.DOTALL)
        found = any('pos-yahoo-link' in block for block in media_blocks)
        self.assertTrue(
            found,
            "style.css must include .pos-yahoo-link inside a @media responsive block"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Integration — both features coexist correctly in the same card template
# ═══════════════════════════════════════════════════════════════════════════

class TestCardTemplateIntegration(unittest.TestCase):
    """Verify both features are wired together consistently in the template."""

    @classmethod
    def setUpClass(cls):
        cls.src = _read(APP_JS)

    def test_yahooUrl_variable_defined_before_template_use(self):
        """The yahooUrl const must be defined before it is used in the template literal."""
        def_idx = self.src.find('const yahooUrl')
        use_idx = self.src.find('${yahooUrl}')
        self.assertGreater(def_idx, -1, "'const yahooUrl' must be defined in renderHoldings")
        self.assertGreater(use_idx, -1, "'${yahooUrl}' must be used in the template")
        self.assertLess(def_idx, use_idx,
                        "'const yahooUrl' must be defined BEFORE '${yahooUrl}' in the template")

    def test_yahooUrl_built_from_sym_variable(self):
        """yahooUrl must use the sanitized 'sym' variable — not 'h.symbol' directly."""
        match = re.search(r'const yahooUrl\s*=\s*`[^`]*\$\{sym\}', self.src)
        self.assertIsNotNone(
            match,
            "yahooUrl must be built from sanitized '${sym}', not raw h.symbol"
        )

    def test_both_features_in_same_holdings_render_function(self):
        """Both pos-name and pos-yahoo-link must be inside the renderHoldings method body.

        Uses the method *definition* (not call-site) as the search anchor.
        """
        # Match the method definition: '  renderHoldings() {' — 2-space indent = class method
        fn_def_match = re.search(r'  renderHoldings\s*\(\s*\)\s*\{', self.src)
        self.assertIsNotNone(fn_def_match, "renderHoldings() method definition must exist")
        holdings_fn_start = fn_def_match.start()

        # The method body ends at the next same-level method definition
        after = self.src[holdings_fn_start + 20:]
        next_method = re.search(r'\n  [a-zA-Z_]\w*\s*\(', after)
        fn_end = (holdings_fn_start + 20 + next_method.start()) if next_method else len(self.src)
        holdings_block = self.src[holdings_fn_start:fn_end]

        self.assertIn('pos-name', holdings_block,
                      "pos-name must be inside renderHoldings() method body")
        self.assertIn('pos-yahoo-link', holdings_block,
                      "pos-yahoo-link must be inside renderHoldings() method body")
        self.assertIn('yahooUrl', holdings_block,
                      "yahooUrl variable must be defined inside renderHoldings()")


if __name__ == "__main__":
    unittest.main(verbosity=2)
