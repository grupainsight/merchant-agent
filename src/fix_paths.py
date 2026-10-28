"""
Merchant Agent - Where an issue is fixed (deterministic, per issue code).

Kept with every issue so that a future "approve and fix" flow can route a
recommendation to the right executor without re-deriving it:
  feed_source               product data in the shop (WooCommerce) that the feed carries
  website                   the product page / server (landing page, crawl)
  merchant_center_settings  account settings in Merchant Center (e.g. local inventory)
  policy_review             a human decision: change the offer or appeal
  google_processing         nothing to fix yet; Google is still reviewing
  merchant_api              attribute override through Merchant API - deliberately unused
                            until the account's data sources are checked: an override can
                            be replaced by the next feed fetch
  unclassified              code not mapped yet
"""

FIX_PATHS = (
    "feed_source", "website", "merchant_center_settings", "policy_review",
    "google_processing", "merchant_api", "unclassified",
)

_BY_CODE = {
    # product page / server
    "landing_page_error": "website",
    "landing_page_pending_crawl": "website",
    "limited_crawling_capacity_issue": "website",
    # product data in the shop
    "item_missing_required_attribute": "feed_source",
    "missing_potentially_required_attribute": "feed_source",
    "title_all_caps": "feed_source",
    "utf8_encoding_error": "feed_source",
    "availability_updated": "feed_source",
    "image_too_small_for_high_resolution": "feed_source",
    "image_too_big": "feed_source",
    "image_link_broken": "feed_source",
    "low_image_quality": "feed_source",
    "image_unwanted_overlays": "feed_source",
    # Merchant Center account settings
    "local_stores_lack_inventory": "merchant_center_settings",
    # Google still reviewing
    "attribute_pending_review": "google_processing",
    # policies: offer change or appeal
    "recalled_product": "policy_review",
    "attribute_violated_discovery_ads_policy": "policy_review",
    "attribute_violating_content_quality_policy": "policy_review",
}


def fix_path(code: str) -> str:
    if code in _BY_CODE:
        return _BY_CODE[code]
    if code.endswith("_policy_violation"):
        return "policy_review"
    return "unclassified"
