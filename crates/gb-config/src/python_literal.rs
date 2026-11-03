//! A Python command's declaration, read the way the Python version reads it
//! (`guildbotics/commands/metadata.py`): the value of its one top-level
//! `COMMAND_METADATA` assignment, a literal (`ast.literal_eval`), without
//! running the module.
//!
//! The literals read are what metadata uses: strings (with their prefixes,
//! escapes and implicit concatenation), numbers, `True`, `False`, `None`, and
//! lists, tuples and dictionaries of them. A declaration the Python version
//! refuses -- more than one, or one that is not a literal mapping with string
//! keys -- is refused here too; so is one holding what is no JSON value (a
//! set, bytes, a complex number, a string with a named escape), and a module
//! this reader cannot read through, rather than read as declaring nothing.

use serde_json::{Map, Number, Value};
use unicode_normalization::UnicodeNormalization;

const NAME: &str = "COMMAND_METADATA";

/// What a module declares: its metadata, and the keys whose value is a tuple
/// rather than a list, which a list-valued field does not accept.
#[derive(Debug, Default, PartialEq)]
pub struct Declaration {
    pub metadata: Map<String, Value>,
    pub tuples: Vec<String>,
}

/// Why a declaration is not read.
#[derive(Debug, PartialEq)]
pub struct Unreadable;

/// The module's declaration: empty when it declares none.
pub fn command_metadata(source: &str) -> Result<Declaration, Unreadable> {
    // A byte order mark is no character of a module Python reads as text.
    if source.starts_with('\u{feff}') {
        return Err(Unreadable);
    }
    // Python reads a module with universal newlines.
    let source = source.replace("\r\n", "\n").replace('\r', "\n");
    let mut declarations = Vec::new();
    for statement in statements(&source)? {
        if let Some(declared) = declared(&statement) {
            declarations.push(declared?);
        }
    }
    match declarations.len() {
        0 => Ok(Declaration::default()),
        1 => Ok(declarations.pop().expect("one")),
        _ => Err(Unreadable),
    }
}

/// What a top-level statement assigns to `COMMAND_METADATA`; `None` when it
/// assigns nothing to it, alone. The statement is read as Python's grammar has
/// it, not by its spelling: its target is what comes before its first `=` (or
/// the `:` of an annotation) outside brackets, a declaration when that is the
/// name alone, in any parentheses (`(COMMAND_METADATA): dict = {...}`).
/// Several targets, a tuple, a subscript, or an augmented assignment declare
/// nothing, as in the Python version.
fn declared(statement: &str) -> Option<Result<Declaration, Unreadable>> {
    let mut lexer = Lexer::new(statement);
    let mut target = Vec::new();
    let annotated = loop {
        match lexer.next_token() {
            Err(e) => return Some(Err(e)),
            Ok(None | Some(Token::Newline)) => return None,
            Ok(Some(Token::Punct('='))) if lexer.depth == 0 => break false,
            Ok(Some(Token::Punct(':'))) if lexer.depth == 0 => break true,
            Ok(Some(token)) => target.push(token),
        }
    };
    if !is_the_name(&target) {
        return None;
    }
    if annotated {
        // `NAME: annotation = value`; an annotation alone assigns nothing. A
        // lambda's parameters, up to its `:`, may hold a `=` of their own.
        let mut in_lambda = false;
        loop {
            match lexer.next_token() {
                Err(e) => return Some(Err(e)),
                Ok(None | Some(Token::Newline)) => return None,
                Ok(Some(Token::Name(name))) if lexer.depth == 0 && name == "lambda" => {
                    in_lambda = true;
                }
                Ok(Some(Token::Punct(':'))) if lexer.depth == 0 => in_lambda = false,
                Ok(Some(Token::Punct('='))) if lexer.depth == 0 && !in_lambda => break,
                Ok(Some(_)) => {}
            }
        }
    }
    let value = &statement[lexer.position..];
    if !annotated && assigns_again(value) {
        return None;
    }
    let mut parser = Parser {
        lexer: Lexer::new(value),
        tuples: Vec::new(),
    };
    let parsed = parser.declaration();
    Some(parsed.and_then(|metadata| {
        if parser.at_end() {
            Ok(Declaration {
                metadata,
                tuples: parser.tuples,
            })
        } else {
            Err(Unreadable)
        }
    }))
}

/// Whether `target` is the declared name alone, in any parentheses; the name
/// compared as Python compares identifiers, normalized to NFKC.
fn is_the_name(target: &[Token]) -> bool {
    let mut inner = target;
    while let [Token::Punct('('), rest @ .., Token::Punct(')')] = inner {
        inner = rest;
    }
    matches!(inner, [Token::Name(name)] if name.nfkc().eq(NAME.chars()))
}

/// Whether what follows a target's `=` is another target and `=`: a chained
/// assignment (`NAME = OTHER = value`), which names several targets.
fn assigns_again(value: &str) -> bool {
    let mut lexer = Lexer::new(value);
    loop {
        match lexer.next_token() {
            Ok(Some(Token::Punct('='))) if lexer.depth == 0 => return true,
            Ok(Some(Token::Name(name))) if !KEYWORDS.contains(&name.as_str()) => {}
            Ok(Some(Token::Punct('(' | ')' | '[' | ']' | ',' | '.' | '*'))) => {}
            _ => return false,
        }
    }
}

/// Keywords that start an expression, which no target is.
const KEYWORDS: &[&str] = &["lambda", "not", "await", "yield", "None", "True", "False"];

/// Python's operators of more than one character, longest first.
const OPERATORS: &[&str] = &[
    "**=", "//=", ">>=", "<<=", "...", "==", "!=", "<=", ">=", ":=", "->", "+=", "-=", "*=", "/=",
    "%=", "&=", "|=", "^=", "@=", "**", "//", "<<", ">>",
];

/// The keywords that start a compound statement, whose line holds its body
/// after the colon, `;`-separated statements and all, never a top-level one.
const COMPOUND: &[&str] = &[
    "if", "elif", "else", "for", "while", "try", "except", "finally", "with", "def", "class",
    "async",
];

/// The module's top-level statements: those of the logical lines that start
/// at the first column (a form feed resets the column), outside any bracket or
/// string, split at their `;`; none of a compound statement's line.
fn statements(source: &str) -> Result<Vec<String>, Unreadable> {
    top_statements(source, false)
}

fn top_statements(source: &str, include_functions: bool) -> Result<Vec<String>, Unreadable> {
    // A form feed sets the column back to zero: what came before it counts
    // for nothing.
    let at_column_zero = |line: &str| {
        let indentation = line.len() - line.trim_start_matches([' ', '\t', '\x0c']).len();
        let after_form_feed = line[..indentation].rfind('\x0c').map_or(0, |i| i + 1);
        indentation == after_form_feed
    };
    let mut statements = Vec::new();
    let mut lexer = Lexer::new(source);
    let mut start = 0;
    let mut top = at_column_zero(source);
    let mut first = true;
    loop {
        let before = lexer.position;
        let token = lexer.next_token()?;
        if first
            && let Some(Token::Name(word)) = &token
            && COMPOUND.contains(&word.as_str())
            && !(include_functions && matches!(word.as_str(), "def" | "async"))
        {
            top = false;
        }
        first = false;
        let ends_line = matches!(token, None | Some(Token::Newline));
        let ends_statement = ends_line || (token == Some(Token::Punct(';')) && lexer.depth == 0);
        if ends_statement {
            if top {
                statements.push(source[start..before].trim().to_string());
            }
            start = lexer.position;
        }
        if token.is_none() {
            return Ok(statements);
        }
        if ends_line {
            top = at_column_zero(&source[start..]);
            first = true;
        }
    }
}

#[derive(Debug, PartialEq)]
enum Token {
    Str(String),
    Number(String),
    Name(String),
    /// A string that is no literal of a JSON value: bytes, an f-string, a
    /// template string, or one with a named escape.
    Opaque,
    Punct(char),
    /// An operator of more than one character (`==`, `:=`, `+=`, ...).
    Op(&'static str),
    Newline,
}

/// The top-level main signature, read without importing the command.
fn main_tokens(source: &str) -> Option<Vec<Token>> {
    for statement in top_statements(source, true).ok()? {
        let mut lexer = Lexer::new(&statement);
        let Some(mut first) = lexer.next_token().ok()? else {
            continue;
        };
        if first == Token::Name("async".into()) {
            first = lexer.next_token().ok()??;
        }
        if first != Token::Name("def".into())
            || lexer.next_token().ok()?? != Token::Name("main".into())
        {
            continue;
        }
        if lexer.next_token().ok()?? != Token::Punct('(') {
            return None;
        }
        let mut tokens = Vec::new();
        while let Some(token) = lexer.next_token().ok()? {
            if token == Token::Punct(')') && lexer.depth == 0 {
                return Some(tokens);
            }
            tokens.push(token);
        }
    }
    None
}

pub(crate) fn screen_arguments(source: &str) -> Value {
    let Some(tokens) = main_tokens(source) else {
        return serde_json::json!([]);
    };
    let mut groups = Vec::new();
    let (mut start, mut depth) = (0, 0);
    for (i, token) in tokens.iter().enumerate() {
        match token {
            Token::Punct('(' | '[' | '{') => depth += 1,
            Token::Punct(')' | ']' | '}') => depth -= 1,
            Token::Punct(',') if depth == 0 => {
                groups.push(&tokens[start..i]);
                start = i + 1;
            }
            _ => {}
        }
    }
    groups.push(&tokens[start..]);
    let mut keyword = false;
    let mut index = 0;
    let mut args = Vec::new();
    for group in groups {
        if group.is_empty() || group == [Token::Punct('/')] {
            continue;
        }
        if group.first() == Some(&Token::Punct('*')) || group.first() == Some(&Token::Op("**")) {
            keyword = true;
            continue;
        }
        let Some(Token::Name(name)) = group.first() else {
            return serde_json::json!([]);
        };
        let default = group
            .iter()
            .position(|t| *t == Token::Punct('='))
            .map(|i| &group[i + 1..]);
        if index == 0 && !keyword && ["context", "ctx", "c"].contains(&name.as_str()) {
            index += 1;
            continue;
        }
        index += 1;
        let text = match default {
            Some([Token::Str(s)]) => s.clone(),
            Some([Token::Number(s)]) => s
                .parse::<serde_json::Number>()
                .map(|n| n.to_string())
                .unwrap_or_default(),
            Some([Token::Name(s)]) if s == "True" || s == "False" => s.clone(),
            _ => String::new(),
        };
        args.push(serde_json::json!({"name":name, "kind":if keyword {"keyword"} else {"positional"}, "required":default.is_none(), "default":text}));
    }
    args.into()
}

pub(crate) fn main_description(source: &str) -> String {
    // A main docstring starts the suite immediately after its signature.
    let Ok(statements) = top_statements(source, true) else {
        return String::new();
    };
    for statement in statements {
        let mut lexer = Lexer::new(&statement);
        let mut first = lexer.next_token().ok().flatten();
        if first == Some(Token::Name("async".into())) {
            first = lexer.next_token().ok().flatten();
        }
        if first != Some(Token::Name("def".into()))
            || lexer.next_token().ok().flatten() != Some(Token::Name("main".into()))
        {
            continue;
        }
        while let Ok(Some(token)) = lexer.next_token() {
            if token == Token::Punct(':') && lexer.depth == 0 {
                if let Ok(Some(Token::Str(doc))) = lexer.next_token() {
                    return doc.trim().lines().next().unwrap_or("").to_owned();
                }
                // The indented suite lies after this top-level statement.
                let offset = source.find(&statement).unwrap_or(0) + statement.len();
                let mut body = Lexer::new(source[offset..].trim_start());
                if let Ok(Some(Token::Str(doc))) = body.next_token() {
                    return doc.trim().lines().next().unwrap_or("").to_owned();
                }
                break;
            }
        }
    }
    String::new()
}

struct Lexer<'a> {
    text: &'a str,
    position: usize,
    depth: usize,
}

impl<'a> Lexer<'a> {
    fn new(text: &'a str) -> Lexer<'a> {
        Lexer {
            text,
            position: 0,
            depth: 0,
        }
    }

    fn rest(&self) -> &'a str {
        &self.text[self.position..]
    }

    fn peek(&self) -> Option<char> {
        self.rest().chars().next()
    }

    fn bump(&mut self) -> Option<char> {
        let c = self.peek()?;
        self.position += c.len_utf8();
        Some(c)
    }

    fn peek_token(&mut self) -> Result<Option<Token>, Unreadable> {
        let (position, depth) = (self.position, self.depth);
        let token = self.next_token();
        (self.position, self.depth) = (position, depth);
        token
    }

    fn next_token(&mut self) -> Result<Option<Token>, Unreadable> {
        loop {
            match self.peek() {
                None => return Ok(None),
                Some(' ' | '\t' | '\x0c') => {
                    self.bump();
                }
                Some('#') => {
                    while !matches!(self.peek(), None | Some('\n')) {
                        self.bump();
                    }
                }
                Some('\\') if self.rest()[1..].starts_with('\n') => {
                    self.position += 2;
                }
                Some('\n') => {
                    self.bump();
                    if self.depth == 0 {
                        return Ok(Some(Token::Newline));
                    }
                }
                Some(_) => break,
            }
        }
        let c = self.peek().expect("a character");
        // An identifier, by the characters Python's identifiers are made of.
        if unicode_ident::is_xid_start(c) || c == '_' {
            let start = self.position;
            while self.peek().is_some_and(unicode_ident::is_xid_continue) {
                self.bump();
            }
            let word = &self.text[start..self.position];
            if matches!(self.peek(), Some('\'' | '"'))
                && word.len() <= 2
                && word.chars().all(|c| "rRuUbBfFtT".contains(c))
            {
                return self.string(word).map(Some);
            }
            return Ok(Some(Token::Name(word.to_string())));
        }
        if c.is_ascii_digit()
            || (c == '.' && self.rest()[1..].starts_with(|c: char| c.is_ascii_digit()))
        {
            let start = self.position;
            while self
                .peek()
                .is_some_and(|c| c.is_ascii_alphanumeric() || c == '_' || c == '.')
            {
                let c = self.bump().expect("a character");
                if matches!(c, 'e' | 'E') && matches!(self.peek(), Some('+' | '-')) {
                    self.bump();
                }
            }
            return Ok(Some(Token::Number(
                self.text[start..self.position].to_string(),
            )));
        }
        if c == '\'' || c == '"' {
            return self.string("").map(Some);
        }
        if let Some(op) = OPERATORS.iter().find(|op| self.rest().starts_with(**op)) {
            self.position += op.len();
            return Ok(Some(Token::Op(op)));
        }
        self.bump();
        match c {
            '(' | '[' | '{' => self.depth += 1,
            ')' | ']' | '}' => self.depth = self.depth.checked_sub(1).ok_or(Unreadable)?,
            _ => {}
        }
        Ok(Some(Token::Punct(c)))
    }

    /// A string literal after its prefix, decoded.
    fn string(&mut self, prefix: &str) -> Result<Token, Unreadable> {
        let prefix = prefix.to_ascii_lowercase();
        let formatted = prefix.contains('f') || prefix.contains('t');
        let mut opaque = formatted || prefix.contains('b');
        let raw = prefix.contains('r');
        let quote = self.bump().ok_or(Unreadable)?;
        let closing = format!("{quote}{quote}{quote}");
        let triple = self.rest().starts_with(&closing[1..]);
        if triple {
            self.position += 2;
        }
        let mut value = String::new();
        loop {
            if (triple && self.rest().starts_with(&closing))
                || (!triple && self.peek() == Some(quote))
            {
                self.position += if triple { 3 } else { 1 };
                return Ok(if opaque {
                    Token::Opaque
                } else {
                    Token::Str(value)
                });
            }
            let c = self.bump().ok_or(Unreadable)?;
            if c == '\n' && !triple {
                return Err(Unreadable);
            }
            if formatted && c == '{' {
                if self.peek() == Some('{') {
                    self.bump();
                } else {
                    self.replacement_field()?;
                }
                continue;
            }
            if c != '\\' {
                value.push(c);
                continue;
            }
            let escaped = self.bump().ok_or(Unreadable)?;
            if raw {
                value.push('\\');
                value.push(escaped);
                continue;
            }
            match escaped {
                '\n' => {}
                '\\' | '\'' | '"' => value.push(escaped),
                'n' => value.push('\n'),
                't' => value.push('\t'),
                'r' => value.push('\r'),
                'a' => value.push('\x07'),
                'b' => value.push('\x08'),
                'f' => value.push('\x0c'),
                'v' => value.push('\x0b'),
                'x' => value.push(self.code_point(2)?),
                'u' => value.push(self.code_point(4)?),
                'U' => value.push(self.code_point(8)?),
                '0'..='7' => {
                    let mut code = escaped.to_digit(8).expect("an octal digit");
                    for _ in 0..2 {
                        match self.peek().and_then(|c| c.to_digit(8)) {
                            Some(digit) => {
                                self.bump();
                                code = code * 8 + digit;
                            }
                            None => break,
                        }
                    }
                    value.push(char::from_u32(code).ok_or(Unreadable)?);
                }
                // A character by its Unicode name, which is not looked up.
                'N' => {
                    opaque = true;
                    while !matches!(self.bump(), None | Some('}')) {}
                }
                other => {
                    value.push('\\');
                    value.push(other);
                }
            }
        }
    }

    /// An f-string's `{...}`: its expression read through as tokens (it may
    /// hold strings with the f-string's own quote, and brackets), then its
    /// format spec, after a `:` of the field's own, as text with fields of
    /// its own.
    fn replacement_field(&mut self) -> Result<(), Unreadable> {
        let outer = self.depth;
        self.depth += 1;
        while self.depth > outer {
            match self.next_token()?.ok_or(Unreadable)? {
                // A `:` of the field's own starts its format spec, even
                // before a `=`: a walrus in a field is in parentheses.
                Token::Punct(':') | Token::Op(":=") if self.depth == outer + 1 => {
                    loop {
                        match self.bump().ok_or(Unreadable)? {
                            '{' => self.replacement_field()?,
                            '}' => break,
                            _ => {}
                        }
                    }
                    self.depth = outer;
                }
                _ => {}
            }
        }
        Ok(())
    }

    fn code_point(&mut self, digits: usize) -> Result<char, Unreadable> {
        let hex = self.rest().get(..digits).ok_or(Unreadable)?;
        let code = u32::from_str_radix(hex, 16).map_err(|_| Unreadable)?;
        self.position += digits;
        char::from_u32(code).ok_or(Unreadable)
    }
}

struct Parser<'a> {
    lexer: Lexer<'a>,
    tuples: Vec<String>,
}

impl Parser<'_> {
    fn at_end(&mut self) -> bool {
        matches!(self.lexer.next_token(), Ok(None | Some(Token::Newline)))
    }

    fn next(&mut self) -> Result<Token, Unreadable> {
        self.lexer.next_token()?.ok_or(Unreadable)
    }

    /// The declared mapping: string keys, the keys of tuples noted.
    fn declaration(&mut self) -> Result<Map<String, Value>, Unreadable> {
        let mut parens = 0;
        while self.lexer.peek_token()? == Some(Token::Punct('(')) {
            self.next()?;
            parens += 1;
        }
        if self.next()? != Token::Punct('{') {
            return Err(Unreadable);
        }
        let mut metadata = Map::new();
        while self.lexer.peek_token()? != Some(Token::Punct('}')) {
            let Value::String(key) = self.value()?.0 else {
                return Err(Unreadable);
            };
            if self.next()? != Token::Punct(':') {
                return Err(Unreadable);
            }
            let (value, tuple) = self.value()?;
            self.tuples.retain(|k| *k != key);
            if tuple {
                self.tuples.push(key.clone());
            }
            metadata.insert(key, value);
            match self.next()? {
                Token::Punct(',') => {}
                Token::Punct('}') => {
                    return self.close(parens).map(|()| metadata);
                }
                _ => return Err(Unreadable),
            }
        }
        self.next()?;
        self.close(parens).map(|()| metadata)
    }

    fn close(&mut self, parens: usize) -> Result<(), Unreadable> {
        for _ in 0..parens {
            if self.next()? != Token::Punct(')') {
                return Err(Unreadable);
            }
        }
        Ok(())
    }

    /// A literal, and whether it is a tuple.
    fn value(&mut self) -> Result<(Value, bool), Unreadable> {
        let value = match self.next()? {
            Token::Str(first) => {
                let mut text = first;
                loop {
                    match self.lexer.peek_token()? {
                        Some(Token::Str(_)) => {
                            let Token::Str(next) = self.next()? else {
                                unreachable!("peeked")
                            };
                            text.push_str(&next);
                        }
                        Some(Token::Opaque) => return Err(Unreadable),
                        _ => break,
                    }
                }
                Value::String(text)
            }
            Token::Number(number) => number_value(&number, false)?,
            Token::Punct(sign @ ('-' | '+')) => match self.next()? {
                Token::Number(number) => number_value(&number, sign == '-')?,
                _ => return Err(Unreadable),
            },
            Token::Name(name) => match name.as_str() {
                "True" => Value::Bool(true),
                "False" => Value::Bool(false),
                "None" => Value::Null,
                _ => return Err(Unreadable),
            },
            Token::Punct('[') => Value::Array(self.items(']')?),
            Token::Punct('(') => {
                if self.lexer.peek_token()? == Some(Token::Punct(')')) {
                    self.next()?;
                    return Ok((Value::Array(Vec::new()), true));
                }
                let (first, tuple) = self.value()?;
                match self.next()? {
                    // A parenthesized value, not a tuple.
                    Token::Punct(')') => return Ok((first, tuple)),
                    Token::Punct(',') => {
                        let mut items = vec![first];
                        items.extend(self.items(')')?);
                        return Ok((Value::Array(items), true));
                    }
                    _ => return Err(Unreadable),
                }
            }
            Token::Punct('{') => self.mapping()?,
            _ => return Err(Unreadable),
        };
        Ok((value, false))
    }

    /// The items of a list or tuple up to `close`, a trailing comma allowed.
    fn items(&mut self, close: char) -> Result<Vec<Value>, Unreadable> {
        let mut items = Vec::new();
        loop {
            if self.lexer.peek_token()? == Some(Token::Punct(close)) {
                self.next()?;
                return Ok(items);
            }
            items.push(self.value()?.0);
            match self.next()? {
                Token::Punct(',') => {}
                Token::Punct(c) if c == close => return Ok(items),
                _ => return Err(Unreadable),
            }
        }
    }

    /// A dictionary within the declaration, its keys spelled as JSON spells
    /// them (`1`, `true`, `null`); a set (`{a, b}`) is not read.
    fn mapping(&mut self) -> Result<Value, Unreadable> {
        let mut map = Map::new();
        loop {
            if self.lexer.peek_token()? == Some(Token::Punct('}')) {
                self.next()?;
                return Ok(Value::Object(map));
            }
            let key = match self.value()?.0 {
                Value::String(key) => key,
                Value::Null => "null".into(),
                key @ (Value::Bool(_) | Value::Number(_)) => key.to_string(),
                _ => return Err(Unreadable),
            };
            if self.next()? != Token::Punct(':') {
                return Err(Unreadable);
            }
            map.insert(key, self.value()?.0);
            match self.next()? {
                Token::Punct(',') => {}
                Token::Punct('}') => return Ok(Value::Object(map)),
                _ => return Err(Unreadable),
            }
        }
    }
}

/// A decimal, hexadecimal, octal or binary integer, or a finite float; a
/// complex number is not read.
fn number_value(text: &str, negative: bool) -> Result<Value, Unreadable> {
    let lower = text.replace('_', "").to_ascii_lowercase();
    let radix = match lower.get(..2) {
        Some("0x") => Some(16),
        Some("0o") => Some(8),
        Some("0b") => Some(2),
        _ => None,
    };
    let sign: i32 = if negative { -1 } else { 1 };
    if let Some(radix) = radix {
        let value = i64::from_str_radix(&lower[2..], radix).map_err(|_| Unreadable)?;
        return Ok(Value::Number((i64::from(sign) * value).into()));
    }
    if lower.ends_with('j') {
        return Err(Unreadable);
    }
    if let Ok(value) = lower.parse::<i64>() {
        return Ok(Value::Number((i64::from(sign) * value).into()));
    }
    let value = lower.parse::<f64>().map_err(|_| Unreadable)?;
    Number::from_f64(f64::from(sign) * value)
        .map(Value::Number)
        .ok_or(Unreadable)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn read(source: &str) -> Result<Value, Unreadable> {
        command_metadata(source).map(|d| Value::Object(d.metadata))
    }

    #[test]
    fn the_declaration_is_the_literal_assigned_at_the_top() {
        let source = r#""""A command.

COMMAND_METADATA = "in the docstring"
"""

import os  # COMMAND_METADATA = "a comment"

COMMAND_METADATA: dict[str, object] = {
    "description": {
        "en": "Inspect " "a ticket.",  # implicit concatenation
        'ja': 'チケット\tを\u78ba認',
    },
    "read_only": True,
    "inspects": ["diagnostics", "config",],
    "numbers": (1, -2, 0x10, 1_000, 2.5e1, None),
}


def main():
    COMMAND_METADATA = {"read_only": False}
"#;
        let declared = command_metadata(source).unwrap();
        assert_eq!(
            Value::Object(declared.metadata),
            json!({
                "description": {"en": "Inspect a ticket.", "ja": "チケット\tを確認"},
                "read_only": true,
                "inspects": ["diagnostics", "config"],
                "numbers": [1, -2, 16, 1000, 25.0, null],
            })
        );
        assert_eq!(declared.tuples, ["numbers"]);
    }

    #[test]
    fn a_module_without_one_declares_nothing() {
        for source in [
            "",
            "COMMAND_METADATA: dict\n",
            "print(COMMAND_METADATA)\n",
            "COMMAND_METADATA_X = {'a': 1}\n",
            "COMMAND_METADATA == {'a': 1}\n",
            "COMMAND_METADATA = OTHER = {'read_only': True}\n",
        ] {
            assert_eq!(read(source), Ok(json!({})), "{source}");
        }
    }

    #[test]
    fn what_is_not_one_readable_literal_mapping_is_refused() {
        for source in [
            "COMMAND_METADATA = dict(read_only=True)\n",
            "COMMAND_METADATA = {1: True}\n",
            "COMMAND_METADATA = {'a', 'b'}\n",
            "COMMAND_METADATA = {'a': b'x'}\n",
            "COMMAND_METADATA = {'a': f'{x}'}\n",
            "COMMAND_METADATA = {'a': '\\N{BULLET}'}\n",
            "COMMAND_METADATA = {'a': 1j}\n",
            "COMMAND_METADATA = {'a': 1e400}\n",
            "COMMAND_METADATA = ['read_only']\n",
            "COMMAND_METADATA = {'read_only': True} + {}\n",
            "COMMAND_METADATA = {'a': 1}\nCOMMAND_METADATA = {'b': 2}\n",
            "x = 'unterminated\nCOMMAND_METADATA = {'read_only': True}\n",
            "\u{feff}COMMAND_METADATA = {'read_only': True}\n",
        ] {
            assert_eq!(read(source), Err(Unreadable), "{source}");
        }
    }

    #[test]
    fn statements_are_found_as_python_reads_them() {
        let read_only = Ok(json!({"read_only": true}));
        for source in [
            "COMMAND_METADATA = ({'read_only': True})\n",
            "import os; COMMAND_METADATA = {'read_only': True}\n",
            "COMMAND_METADATA = {'read_only': True};\n",
            "COMMAND_METADATA = \\\r\n{'read_only': True}\r\n",
            "X = f\"{d[\"it's\"]}\"\nCOMMAND_METADATA = {'read_only': True}\n",
            "v = f\"{fn(\"(\")}\"\nCOMMAND_METADATA = {'read_only': True}\n",
            "v = f\"{x:>{w}} {{literal}}\"\nCOMMAND_METADATA = {'read_only': True}\n",
            "@decorated\ndef f():\n    pass\n\nCOMMAND_METADATA = {'read_only': True}\n",
            "if __name__ == '__main__':\n    COMMAND_METADATA = {}\nCOMMAND_METADATA = {'read_only': True}\n",
            "n = 1\nv = f\"{n:#06x} {n:'^10} {n!r:>{n}}\"\nCOMMAND_METADATA = {'read_only': True}\n",
            "COMMAND_METADATA: Annotated[dict, F(a=1)] = {'read_only': True}\n",
            "if True: pass; COMMAND_METADATA = {}\nCOMMAND_METADATA = {'read_only': True}\n",
            "class C: x = 1; COMMAND_METADATA = {}\nCOMMAND_METADATA = {'read_only': True}\n",
            "def f():\n    pass\n\x0c    COMMAND_METADATA = {}\nCOMMAND_METADATA = {'read_only': True}\n",
        ] {
            assert_eq!(read(source), read_only, "{source}");
        }
        assert_eq!(
            read("COMMAND_METADATA = {'d': \"\"\"a\r\nb\"\"\", 'k': {1: 'x', None: 'y'}}\r\n"),
            Ok(json!({"d": "a\nb", "k": {"1": "x", "null": "y"}}))
        );
        assert_eq!(
            read("COMMAND_METADATA = {'a': r'\\d'}\n"),
            Ok(json!({"a": "\\d"}))
        );
    }
}
