// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

//! Operator-pinned OpenShell extension JWT verification, independent of context lookup.
//!
//! Keys come only from the configured public JWKS file. No URL or key embedded in
//! an incoming token is followed. The gateway reuses tokens; `jti` is not a nonce.
use crate::strict_json;
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use jsonwebtoken::{Algorithm, DecodingKey, Validation, decode, decode_header};
use serde::Deserialize;
use std::{
    collections::BTreeMap,
    time::{SystemTime, UNIX_EPOCH},
};
use tonic::{Status, metadata::MetadataMap};

/// Pinned gateway public keys and the one exact extension audience accepted here.
pub struct ExtensionAuth {
    keys: BTreeMap<String, DecodingKey>,
    issuer: String,
    audience: String,
}

#[derive(Deserialize)]
struct Claims {
    iss: String,
    aud: String,
    sub: String,
    iat: u64,
    exp: u64,
    jti: String,
    caller_kind: String,
    sandbox_id: Option<String>,
}

impl ExtensionAuth {
    /// Accept a bounded, operator-provided public Ed25519 JWKS, gateway ID and audience.
    /// Rotating the pinned keys requires updating this file and restarting the service.
    pub fn from_jwks(bytes: &[u8], gateway_id: &str, audience: &str) -> Result<Self, &'static str> {
        if bytes.len() > 65_536
            || gateway_id.is_empty()
            || gateway_id.len() > 256
            || gateway_id.trim() != gateway_id
            || audience.trim() != audience
            || audience.is_empty()
            || audience.len() > 512
            || gateway_id.chars().any(char::is_control)
            || audience.chars().any(char::is_control)
        {
            return Err("invalid_extension_auth_configuration");
        }
        let value = strict_json::parse(bytes).map_err(|_| "invalid_extension_jwks")?;
        let entries = value
            .get("keys")
            .and_then(|v| v.as_array())
            .ok_or("invalid_extension_jwks")?;
        if entries.is_empty() || entries.len() > 16 {
            return Err("invalid_extension_jwks");
        }
        let mut keys = BTreeMap::new();
        for entry in entries {
            if entry.get("kty").and_then(|v| v.as_str()) != Some("OKP")
                || entry.get("crv").and_then(|v| v.as_str()) != Some("Ed25519")
                || entry.get("d").is_some()
                || entry
                    .get("alg")
                    .is_some_and(|v| v.as_str() != Some("EdDSA"))
                || entry.get("use").is_some_and(|v| v.as_str() != Some("sig"))
            {
                return Err("invalid_extension_jwks");
            }
            let kid = entry
                .get("kid")
                .and_then(|v| v.as_str())
                .filter(|v| !v.is_empty() && v.len() <= 256)
                .ok_or("invalid_extension_jwks")?;
            let x = entry
                .get("x")
                .and_then(|v| v.as_str())
                .ok_or("invalid_extension_jwks")?;
            if URL_SAFE_NO_PAD
                .decode(x)
                .map_err(|_| "invalid_extension_jwks")?
                .len()
                != 32
            {
                return Err("invalid_extension_jwks");
            }
            let key = DecodingKey::from_ed_components(x).map_err(|_| "invalid_extension_jwks")?;
            if keys.insert(kid.to_string(), key).is_some() {
                return Err("invalid_extension_jwks");
            }
        }
        Ok(Self {
            keys,
            issuer: format!("openshell-gateway:{gateway_id}"),
            audience: audience.into(),
        })
    }

    /// Advertise the exact audience so OpenShell rejects a mismatched registration.
    pub fn audience(&self) -> &str {
        &self.audience
    }

    fn claims(&self, metadata: &MetadataMap) -> Result<Claims, Status> {
        let invalid = || Status::unauthenticated("invalid_extension_credentials");
        if metadata.get_all("authorization").iter().count() != 1 {
            return Err(invalid());
        }
        let token = metadata
            .get("authorization")
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.strip_prefix("Bearer "))
            .filter(|v| !v.is_empty() && v.len() <= 16_384)
            .ok_or_else(invalid)?;
        let parts: Vec<_> = token.split('.').collect();
        if parts.len() != 3 {
            return Err(invalid());
        }
        // Reject ambiguous duplicate claims/headers before the JWT library decodes them.
        for part in &parts[..2] {
            let decoded = URL_SAFE_NO_PAD.decode(part).map_err(|_| invalid())?;
            let value = strict_json::parse(&decoded).map_err(|_| invalid())?;
            if !value.is_object() {
                return Err(invalid());
            }
        }
        let header = decode_header(token).map_err(|_| invalid())?;
        if header.alg != Algorithm::EdDSA
            || header.typ.as_deref() != Some("openshell-ext+jwt")
            || header.crit.as_ref().is_some_and(|v| !v.is_empty())
        {
            return Err(invalid());
        }
        let key = header
            .kid
            .as_ref()
            .and_then(|kid| self.keys.get(kid))
            .ok_or_else(invalid)?;
        let mut validation = Validation::new(Algorithm::EdDSA);
        validation.leeway = 0;
        validation.validate_nbf = true;
        validation.set_issuer(&[&self.issuer]);
        validation.set_audience(&[&self.audience]);
        validation.set_required_spec_claims(&["iss", "aud", "sub", "exp", "iat", "jti"]);
        let claims = decode::<Claims>(token, key, &validation)
            .map_err(|_| invalid())?
            .claims;
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| invalid())?
            .as_secs();
        if claims.iss != self.issuer
            || claims.aud != self.audience
            || claims.iat > now
            || claims.exp <= now
            || claims.exp <= claims.iat
            || claims.exp - claims.iat > 3600
            || claims.jti.is_empty()
        {
            return Err(invalid());
        }
        Ok(claims)
    }

    /// Authorize control RPCs from either OpenShell caller profile.
    /// Supervisors also perform discovery/config validation when loading their registry.
    /// This never authorizes a context lookup without an HTTP request's sandbox binding.
    pub fn authorize_control_rpc(&self, metadata: &MetadataMap) -> Result<(), Status> {
        let claims = self.claims(metadata)?;
        let valid = match claims.caller_kind.as_str() {
            "gateway" => claims.sandbox_id.is_none() && claims.sub == self.issuer,
            "supervisor" => claims.sandbox_id.as_ref().is_some_and(|id| {
                !id.is_empty() && claims.sub == format!("spiffe://openshell/sandbox/{id}")
            }),
            _ => false,
        };
        if !valid {
            return Err(Status::permission_denied("extension_caller_mismatch"));
        }
        Ok(())
    }

    /// Authorize a supervisor only for its JWT-bound sandbox ID, never a chosen name alone.
    pub fn authorize_supervisor(
        &self,
        metadata: &MetadataMap,
        sandbox_id: &str,
    ) -> Result<(), Status> {
        let claims = self.claims(metadata)?;
        if sandbox_id.is_empty()
            || claims.caller_kind != "supervisor"
            || claims.sandbox_id.as_deref() != Some(sandbox_id)
            || claims.sub != format!("spiffe://openshell/sandbox/{sandbox_id}")
        {
            return Err(Status::permission_denied("extension_caller_mismatch"));
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use jsonwebtoken::{EncodingKey, Header, encode};
    use ring::{
        rand::SystemRandom,
        signature::{Ed25519KeyPair, KeyPair},
    };
    use serde_json::{Value, json};

    fn identity() -> (ExtensionAuth, EncodingKey) {
        let private = Ed25519KeyPair::generate_pkcs8(&SystemRandom::new()).unwrap();
        let pair = Ed25519KeyPair::from_pkcs8(private.as_ref()).unwrap();
        let jwks = json!({"keys":[{"kty":"OKP","crv":"Ed25519","kid":"test-key",
            "x":URL_SAFE_NO_PAD.encode(pair.public_key().as_ref()),"alg":"EdDSA","use":"sig"}]});
        (
            ExtensionAuth::from_jwks(
                &serde_json::to_vec(&jwks).unwrap(),
                "test-gateway",
                "urn:test",
            )
            .unwrap(),
            EncodingKey::from_ed_der(private.as_ref()),
        )
    }
    fn claims(kind: &str) -> Value {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs();
        let mut value = json!({"iss":"openshell-gateway:test-gateway","aud":"urn:test", "iat":now,
            "exp":now+300,"jti":"reused-by-gateway","caller_kind":kind,
            "sub":"openshell-gateway:test-gateway"});
        if kind == "supervisor" {
            value["sandbox_id"] = json!("sandbox-A");
            value["sub"] = json!("spiffe://openshell/sandbox/sandbox-A");
        }
        value
    }
    fn metadata(key: &EncodingKey, value: &Value, typ: &str) -> MetadataMap {
        let mut header = Header::new(Algorithm::EdDSA);
        header.kid = Some("test-key".into());
        header.typ = Some(typ.into());
        let token = encode(&header, value, key).unwrap();
        let mut metadata = MetadataMap::new();
        metadata.insert("authorization", format!("Bearer {token}").parse().unwrap());
        metadata
    }
    /// A reusable gateway JWT is valid for discovery but never supervisor assessment.
    #[test]
    fn gateway_tokens_have_a_distinct_profile_and_can_be_reused() {
        let (auth, key) = identity();
        let metadata = metadata(&key, &claims("gateway"), "openshell-ext+jwt");
        assert!(auth.authorize_control_rpc(&metadata).is_ok());
        assert!(auth.authorize_control_rpc(&metadata).is_ok());
        assert!(auth.authorize_supervisor(&metadata, "sandbox-A").is_err());
    }
    /// Supervisor identity must agree with the request, independently of its sandbox name.
    #[test]
    fn supervisor_tokens_are_bound_to_one_sandbox() {
        let (auth, key) = identity();
        let metadata = metadata(&key, &claims("supervisor"), "openshell-ext+jwt");
        assert!(auth.authorize_supervisor(&metadata, "sandbox-A").is_ok());
        assert!(auth.authorize_supervisor(&metadata, "sandbox-B").is_err());
        assert!(auth.authorize_control_rpc(&metadata).is_ok());
    }
    /// Reject wrong issuer/audience, expired/long/future credentials and missing claims.
    #[test]
    fn altered_claims_are_not_authorized() {
        let (auth, key) = identity();
        for (name, value) in [
            ("iss", json!("openshell-gateway:other")),
            ("aud", json!("wrong")),
            ("exp", json!(1)),
            ("exp", json!(u64::MAX)),
            ("iat", json!(u64::MAX)),
            ("caller_kind", json!("gateway")),
            ("sub", json!("other")),
            ("jti", json!("")),
        ] {
            let mut c = claims("supervisor");
            c[name] = value;
            assert!(
                auth.authorize_supervisor(&metadata(&key, &c, "openshell-ext+jwt"), "sandbox-A")
                    .is_err(),
                "{name}"
            );
        }
        let mut c = claims("supervisor");
        c.as_object_mut().unwrap().remove("iat");
        assert!(
            auth.authorize_supervisor(&metadata(&key, &c, "openshell-ext+jwt"), "sandbox-A")
                .is_err()
        );
    }
    /// Other JWT profiles, other signing keys and absent credentials cannot disclose context.
    #[test]
    fn signature_typ_and_credentials_are_enforced() {
        let (auth, key) = identity();
        let (_, wrong_key) = identity();
        let c = claims("supervisor");
        assert!(
            auth.authorize_supervisor(&metadata(&key, &c, "JWT"), "sandbox-A")
                .is_err()
        );
        assert!(
            auth.authorize_supervisor(&metadata(&wrong_key, &c, "openshell-ext+jwt"), "sandbox-A")
                .is_err()
        );
        assert!(
            auth.authorize_supervisor(&MetadataMap::new(), "sandbox-A")
                .is_err()
        );
        let mut duplicated = metadata(&key, &c, "openshell-ext+jwt");
        duplicated.append(
            "authorization",
            duplicated.get("authorization").unwrap().clone(),
        );
        assert!(auth.authorize_supervisor(&duplicated, "sandbox-A").is_err());
    }
    /// Even correctly signed malformed JOSE/claim JSON cannot bypass the pinned profile.
    #[test]
    fn signed_ambiguous_headers_and_claims_are_rejected() {
        let private = Ed25519KeyPair::generate_pkcs8(&SystemRandom::new()).unwrap();
        let pair = Ed25519KeyPair::from_pkcs8(private.as_ref()).unwrap();
        let jwks = json!({"keys":[{"kty":"OKP","crv":"Ed25519","kid":"test-key",
            "x":URL_SAFE_NO_PAD.encode(pair.public_key().as_ref())}]});
        let auth = ExtensionAuth::from_jwks(
            &serde_json::to_vec(&jwks).unwrap(),
            "test-gateway",
            "urn:test",
        )
        .unwrap();
        let good_header = r#"{"alg":"EdDSA","typ":"openshell-ext+jwt","kid":"test-key"}"#;
        let good_claims = serde_json::to_string(&claims("supervisor")).unwrap();
        let duplicate_claims = good_claims.replacen(
            "\"aud\":\"urn:test\"",
            "\"aud\":\"urn:test\",\"aud\":\"wrong\"",
            1,
        );
        for (header, body) in [
            (
                r#"{"alg":"EdDSA","alg":"HS256","typ":"openshell-ext+jwt","kid":"test-key"}"#,
                good_claims.as_str(),
            ),
            (good_header, duplicate_claims.as_str()),
            (
                r#"{"alg":"EdDSA","typ":"openshell-ext+jwt","kid":"test-key","crit":["unknown"]}"#,
                good_claims.as_str(),
            ),
            (
                r#"{"alg":"HS256","typ":"openshell-ext+jwt","kid":"test-key"}"#,
                good_claims.as_str(),
            ),
            (
                r#"{"alg":"EdDSA","typ":"openshell-ext+jwt","kid":"unknown"}"#,
                good_claims.as_str(),
            ),
        ] {
            let signing = format!(
                "{}.{}",
                URL_SAFE_NO_PAD.encode(header),
                URL_SAFE_NO_PAD.encode(body)
            );
            let token = format!(
                "{signing}.{}",
                URL_SAFE_NO_PAD.encode(pair.sign(signing.as_bytes()).as_ref())
            );
            let mut metadata = MetadataMap::new();
            metadata.insert("authorization", format!("Bearer {token}").parse().unwrap());
            assert!(auth.authorize_supervisor(&metadata, "sandbox-A").is_err());
        }
    }

    /// Lifetime/missing claims and oversized credentials remain fail-closed.
    #[test]
    fn future_nbf_missing_jti_and_oversized_credentials_are_rejected() {
        let (auth, key) = identity();
        let mut c = claims("supervisor");
        c["nbf"] = json!(c["exp"].as_u64().unwrap() + 1);
        assert!(
            auth.authorize_supervisor(&metadata(&key, &c, "openshell-ext+jwt"), "sandbox-A")
                .is_err()
        );
        c = claims("supervisor");
        c.as_object_mut().unwrap().remove("jti");
        assert!(
            auth.authorize_supervisor(&metadata(&key, &c, "openshell-ext+jwt"), "sandbox-A")
                .is_err()
        );
        let mut metadata = MetadataMap::new();
        metadata.insert(
            "authorization",
            format!("Bearer {}", "a".repeat(16_385)).parse().unwrap(),
        );
        assert!(auth.authorize_supervisor(&metadata, "sandbox-A").is_err());
    }

    /// Pinned key files are bounded public-only JWKS and cannot supply private/symmetric keys.
    #[test]
    fn malformed_and_private_jwks_are_rejected() {
        for bytes in [
            b"{}".as_slice(),
            b"{\"keys\":[]}",
            b"{\"keys\":[],\"keys\":[]}",
        ] {
            assert!(ExtensionAuth::from_jwks(bytes, "gateway", "urn:test").is_err());
        }
        let key = json!({"keys":[{"kty":"OKP","crv":"Ed25519","kid":"x","x":URL_SAFE_NO_PAD.encode([0u8;32]),"d":"private"}]});
        assert!(
            ExtensionAuth::from_jwks(&serde_json::to_vec(&key).unwrap(), "gateway", "urn:test")
                .is_err()
        );
        assert!(ExtensionAuth::from_jwks(&vec![b' '; 65_537], "gateway", "urn:test").is_err());
    }
}
