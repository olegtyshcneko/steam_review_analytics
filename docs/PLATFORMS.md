# Platform collection notes

Games Analytics separates storefront collection from shared review analysis.
Each adapter emits normalized products and reviews; the v2 labeling contract,
sampling, OpenRouter batch path, aggregation, and report renderer are common.

## Steam

`platforms/steam.py` uses the public Steam Store review endpoint with cursor
pagination, Steam Store metadata, and optional SteamSpy discovery/tag data.
Steam reviews retain platform-specific playtime and helpful-vote fields. Author
Steam IDs are salted and hashed, then removed from retained raw payloads.

## Google Play

`platforms/google_play.py` mines the public storefront review transport with a
continuation token. It accepts an Android package name, language, and country.
The collector retains review text, rating, date, app version, helpful count, and
developer response, but omits reviewer names and profile images.

Google's official
[reviews.list API](https://developers.google.com/android-publisher/api-ref/rest/v3/reviews/list)
requires the Android Publisher OAuth scope and is intended for a developer's own
apps. A future authenticated owner adapter can use that API without changing the
normalized storage or analysis contract.

The public storefront transport is not a supported public API and can change
without notice. Keep request rates conservative and run live smoke tests after
collector changes.

## Apple App Store

`platforms/app_store.py` uses Apple's public lookup response for product metadata
and country-specific public customer-review feeds for competitor research. It
retains review text, title, rating, date, app version, and vote count, but omits
reviewer identity.

The public feed exposes pages 1 through 10 with up to 50 reviews per page, so one
country storefront normally yields at most roughly 500 currently visible
reviews. Mine multiple country storefronts only when the research question
requires them, and keep storefront provenance in the product/review metadata.

Apple's official
[App Store Connect Customer Reviews API](https://developer.apple.com/documentation/appstoreconnectapi/customer-reviews)
requires App Store Connect authorization and covers apps controlled by that
developer account. It is the preferred future owner mode.

## Shared polarity and analysis

Mobile ratings map to the default negative-first sampling policy:

| Rating | Source polarity | Default sample |
|---|---|---|
| 1–2 | Negative | Prioritized |
| 3 | Neutral | Excluded from polarized corpus |
| 4–5 | Positive | Deterministic contrast sample |

Raw reviews are always untrusted input. The model may classify them but must not
follow instructions, links, or tool requests contained in review text.
