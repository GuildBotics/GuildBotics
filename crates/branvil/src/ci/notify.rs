//! Deliver the durable notice created with each repair ticket, outside transactions.
use crate::app::App;
use crate::domain::tickets;
use crate::error::AppResult;
use crate::model::Ticket;
use crate::store::{self, Rd};

pub async fn sweep(app: &App) -> AppResult<()> {
    let candidates = app
        .store
        .read(|r| r.prefix_keys(store::IX_REPAIR_UNNOTIFIED, &[]))
        .await?;
    for key in candidates {
        let read_key = key.clone();
        let notice = app
            .store
            .read(move |r| {
                Ok(r.get::<Ticket>(store::TICKETS, &read_key)?
                    .and_then(|t| t.repair.and_then(|r| r.notice)))
            })
            .await?;
        let Some(notice) = notice else {
            continue;
        };
        if !app.autoland.send(&app.secrets, &notice).await {
            break;
        }
        app.store
            .write(move |w| {
                if let Some(old) = w.get::<Ticket>(store::TICKETS, &key)? {
                    let mut new = old.clone();
                    if let Some(repair) = new.repair.as_mut() {
                        repair.notice = None;
                        tickets::write_ticket(w, Some(&old), &new)?;
                    }
                }
                Ok(())
            })
            .await?;
    }
    Ok(())
}
