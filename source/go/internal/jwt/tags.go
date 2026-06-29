package jwt

// ExtractPrincipalTag returns the value of an AWS session-tag claim. STS and
// the IdP-side recipes in assets/docs/COST_ATTRIBUTION.md both accept two
// shapes:
//
//   - Flat:   {"https://aws.amazon.com/tags/principal_tags/<Key>": "<value>"}
//   - Nested: {"https://aws.amazon.com/tags": {"principal_tags": {"<Key>": "<value>" | ["<value>", ...]}}}
//
// Returns empty string when the tag isn't present or the claim is malformed.
func ExtractPrincipalTag(claims Claims, tagKey string) string {
	if s, ok := claims["https://aws.amazon.com/tags/principal_tags/"+tagKey].(string); ok && s != "" {
		return s
	}

	root, ok := claims["https://aws.amazon.com/tags"].(map[string]interface{})
	if !ok {
		return ""
	}
	principalTags, ok := root["principal_tags"].(map[string]interface{})
	if !ok {
		return ""
	}
	switch v := principalTags[tagKey].(type) {
	case string:
		return v
	case []interface{}:
		if len(v) > 0 {
			if s, ok := v[0].(string); ok {
				return s
			}
		}
	}
	return ""
}
