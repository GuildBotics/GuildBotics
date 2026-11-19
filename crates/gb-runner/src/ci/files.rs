pub fn valid_attempt_id(id: &str) -> bool {
    id.strip_prefix("att_")
        .is_some_and(|u| u.len() == 26 && u.bytes().all(|b| b.is_ascii_alphanumeric()))
}
