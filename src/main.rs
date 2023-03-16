use anyhow::Context;
use async_graphql::{InputValueError, Value};
use nipper::{Document, MatchScope, Matcher, Matches, StrTendril};
use std::sync::{Arc, Mutex};

struct Selector(Matcher, String);

#[async_graphql::Scalar]
impl async_graphql::ScalarType for Selector {
    fn parse(value: Value) -> Result<Self, InputValueError<Self>> {
        if let Value::String(s) = value {
            Matcher::new(&s)
                .ok(/* don't know how to format cssparser::ParseError */)
                .context("invalid css selection string")
                .map_err(InputValueError::custom)
                .map(|m| Selector(m, s))
        } else {
            Err(InputValueError::custom("expected css selection string"))
        }
    }

    fn to_value(&self) -> Value {
        Value::String(self.1.clone())
    }
}

struct Query;

#[async_graphql::Object]
impl Query {
    async fn get(&self, url: String) -> anyhow::Result<Node> {
        let body = ureq::get(&url).call()?.into_string()?;
        let document = Document::from(&body);
        let id = document.root().id;
        let document = Arc::new(Mutex::new(document));
        Ok(Node { document, id })
    }
}

struct Node {
    document: Arc<Mutex<Document>>,
    id: nipper::NodeId,
}

impl Node {
    fn with_node<F, R>(&self, f: F) -> R
    where
        F: FnOnce(nipper::Node) -> R,
    {
        let document = self.document.lock().unwrap();
        let node = document.node(self.id);
        f(node)
    }

    fn attr(&self, attr: &str) -> Option<String> {
        self.with_node(|node| node.attr(attr))
            .as_ref()
            .map(StrTendril::to_string)
    }
}

#[async_graphql::Object]
impl Node {
    async fn this_text(&self) -> Option<String> {
        let document = self.document.lock().unwrap();
        let node = document.node(self.id);
        node.is_text().then(|| node.text().to_string())
    }

    #[graphql(name = "attr")]
    async fn attr_(&self, attr: String) -> Option<String> {
        self.attr(&attr)
    }

    async fn href(&self) -> Option<String> {
        self.attr("href")
    }

    async fn class(&self) -> Vec<String> {
        self.attr("class")
            .map(|s| s.split_ascii_whitespace().map(ToOwned::to_owned).collect())
            .unwrap_or_default()
    }

    async fn text(&self) -> String {
        let document = self.document.lock().unwrap();
        let this = document.node(self.id);
        walk(this)
            .filter(|node| node.is_text())
            .map(|node| node.text().to_string())
            .collect::<String>()
    }

    async fn html(&self) -> String {
        self.with_node(|node| node.html()).to_string()
    }

    async fn name(&self) -> String {
        self.with_node(|node| node.node_name())
            .as_ref()
            .map(StrTendril::to_string)
            .unwrap_or_default()
    }

    async fn select(&self, select: Selector) -> anyhow::Result<Vec<Node>> {
        let Selector(mut matcher, _) = select;
        matcher.scope = Some(self.id);

        let document = self.document.lock().unwrap();
        let node = document.node(self.id);

        Ok(Matches::from_one(node, matcher, MatchScope::IncludeNode)
            .map(|node| Node {
                document: Arc::clone(&self.document),
                id: node.id,
            })
            .collect::<Vec<_>>())
    }
}

fn main() -> anyhow::Result<()> {
    let mut argv = std::env::args();
    let _exe = argv
        .next()
        .unwrap_or_else(|| env!("CARGO_PKG_NAME").to_string());
    let query = argv.next().context("graphql query required")?;

    let vars = {
        use std::io::Read;

        let mut inp = String::new();

        std::io::stdin().lock().read_to_string(&mut inp)?;

        if inp.is_empty() {
            serde_json::Value::Null
        } else {
            serde_json::from_str(&inp).context("parse json variables from stdin")?
        }
    };

    use async_graphql::*;
    let schema = Schema::new(Query, EmptyMutation, EmptySubscription);
    let req = Request::new(query).variables(Variables::from_json(vars));
    let res = extreme::run(schema.execute(req));
    let s = serde_json::to_string(&res.data)?;
    println!("{}", s);

    for err in res.errors.iter() {
        eprintln!("{}", err);
    }

    Ok(())
}

fn walk<'a>(node: nipper::Node<'a>) -> impl Iterator<Item = nipper::Node<'a>> {
    let mut stack = vec![node];

    std::iter::from_fn(move || {
        let next = stack.pop()?;

        /* push children to stack in reverse order */
        let mut child = next.last_child();
        while let Some(some) = child {
            child = some.prev_sibling();
            stack.push(some);
        }

        Some(next)
    })
}
