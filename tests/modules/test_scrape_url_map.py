"""The scrape blueprint's URL surface must not change when handlers move between files."""

from __future__ import annotations

EXPECTED = [
    ("scrape.add_citation_paper", "/papers/<int:user_paper_id>/citation-graph/add", ("POST",)),
    ("scrape.add_link_route", "/papers/add-link", ("POST",)),
    ("scrape.add_note_route", "/papers/<int:user_paper_id>/notes", ("POST",)),
    ("scrape.bluesky_list", "/papers/profile/bluesky", ("GET",)),
    ("scrape.bulk_action", "/papers/bulk-action", ("POST",)),
    ("scrape.channel_list", "/papers/profile/channels", ("GET",)),
    ("scrape.citation_graph", "/papers/<int:user_paper_id>/citation-graph", ("GET",)),
    ("scrape.cite_route", "/papers/<int:user_paper_id>/cite", ("GET",)),
    ("scrape.delete_note_route", "/papers/notes/<int:note_id>/delete", ("POST",)),
    ("scrape.detail", "/papers/<int:user_paper_id>", ("GET",)),
    ("scrape.dismiss", "/papers/<int:user_paper_id>/dismiss", ("POST",)),
    ("scrape.edit_note_route", "/papers/notes/<int:note_id>/edit", ("GET", "POST")),
    ("scrape.export_notes_route", "/papers/<int:user_paper_id>/export-notes", ("GET",)),
    ("scrape.feed", "/papers/", ("GET",)),
    ("scrape.feed_list", "/papers/profile/feeds", ("GET",)),
    ("scrape.generate_analysis_route", "/papers/<int:user_paper_id>/analysis", ("POST",)),
    ("scrape.generate_translation_route", "/papers/<int:user_paper_id>/translate", ("POST",)),
    ("scrape.generate_video_summary_route", "/papers/<int:user_paper_id>/video-summary", ("POST",)),
    ("scrape.open_paper", "/papers/<int:user_paper_id>/open", ("POST",)),
    ("scrape.page_list", "/papers/profile/pages", ("GET",)),
    ("scrape.prior_art", "/papers/patents", ("GET", "POST")),
    ("scrape.report_create", "/papers/reports/new", ("POST",)),
    ("scrape.report_delete", "/papers/reports/<int:report_id>/delete", ("POST",)),
    ("scrape.report_detail", "/papers/reports/<int:report_id>", ("GET",)),
    ("scrape.report_status", "/papers/reports/<int:report_id>/status", ("GET",)),
    ("scrape.reports", "/papers/reports", ("GET",)),
    ("scrape.run_now", "/papers/run", ("POST",)),
    ("scrape.scan_status", "/papers/scan-status", ("GET",)),
    ("scrape.scrape_status_poll", "/papers/status/<task_id>", ("GET",)),
    ("scrape.send_chat_message", "/papers/<int:user_paper_id>/chat", ("POST",)),
    ("scrape.similar", "/papers/<int:user_paper_id>/similar", ("GET",)),
    ("scrape.source_manager", "/papers/profile/source-manager", ("GET",)),
    ("scrape.submit_ai_clear", "/papers/profile/ai/clear", ("POST",)),
    ("scrape.submit_ai_save", "/papers/profile/ai/save", ("POST",)),
    ("scrape.submit_author_add_to_group", "/papers/profile/authors/search/add-to-group", ("POST",)),
    ("scrape.submit_author_delete", "/papers/profile/authors/<int:author_id>/delete", ("POST",)),
    ("scrape.submit_author_follow", "/papers/profile/authors/follow", ("POST",)),
    ("scrape.submit_author_pause", "/papers/profile/authors/<int:author_id>/pause", ("POST",)),
    ("scrape.submit_author_search", "/papers/profile/authors/search", ("POST",)),
    ("scrape.submit_bluesky_add", "/papers/profile/bluesky/add", ("POST",)),
    ("scrape.submit_bluesky_remove", "/papers/profile/bluesky/<int:bluesky_pk>/remove", ("POST",)),
    ("scrape.submit_bluesky_toggle", "/papers/profile/bluesky/<int:bluesky_pk>/toggle", ("POST",)),
    ("scrape.submit_channel_add", "/papers/profile/channels/add", ("POST",)),
    ("scrape.submit_channel_remove", "/papers/profile/channels/<int:channel_pk>/remove", ("POST",)),
    ("scrape.submit_channel_toggle", "/papers/profile/channels/<int:channel_pk>/toggle", ("POST",)),
    ("scrape.submit_feed_add", "/papers/profile/feeds/add", ("POST",)),
    ("scrape.submit_feed_remove", "/papers/profile/feeds/<int:feed_id>/remove", ("POST",)),
    ("scrape.submit_feed_toggle", "/papers/profile/feeds/<int:feed_id>/toggle", ("POST",)),
    ("scrape.submit_group_create", "/papers/profile/authors/groups/create", ("POST",)),
    (
        "scrape.submit_group_delete",
        "/papers/profile/authors/groups/<int:group_id>/delete",
        ("POST",),
    ),
    (
        "scrape.submit_group_member_add",
        "/papers/profile/authors/groups/<int:group_id>/members/add",
        ("POST",),
    ),
    (
        "scrape.submit_group_member_remove",
        "/papers/profile/authors/groups/<int:group_id>/members/<int:member_id>/remove",
        ("POST",),
    ),
    (
        "scrape.submit_group_rename",
        "/papers/profile/authors/groups/<int:group_id>/rename",
        ("POST",),
    ),
    (
        "scrape.submit_group_report",
        "/papers/profile/authors/groups/<int:group_id>/report",
        ("POST",),
    ),
    ("scrape.submit_page_add", "/papers/profile/pages/add", ("POST",)),
    ("scrape.submit_page_remove", "/papers/profile/pages/<int:page_pk>/remove", ("POST",)),
    ("scrape.submit_page_toggle", "/papers/profile/pages/<int:page_pk>/toggle", ("POST",)),
    ("scrape.toggle_favorite_route", "/papers/<int:user_paper_id>/favorite/toggle", ("POST",)),
    ("scrape.toggle_read_later_route", "/papers/<int:user_paper_id>/read-later/toggle", ("POST",)),
    ("scrape.undismiss", "/papers/<int:user_paper_id>/undismiss", ("POST",)),
    ("scrape.view_note_route", "/papers/notes/<int:note_id>/view", ("GET",)),
]


def test_scrape_url_map_is_unchanged(app):
    actual = sorted(
        (r.endpoint, r.rule, tuple(sorted(m for m in r.methods if m not in ("HEAD", "OPTIONS"))))
        for r in app.url_map.iter_rules()
        if r.endpoint.startswith("scrape.") and r.endpoint != "scrape.static"
    )
    assert actual == sorted(EXPECTED)
