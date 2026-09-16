#pragma once

#include "auth/AuthManager.h"
#include "core/config/AppConfig.h"
#include "services/llm/LlmService.h"
#include "services/llm/ModelCatalog.h"
#include "services/llm/ProviderCatalog.h"

#include <QJsonObject>

namespace fincept::services::equity {

/// Snapshot Fincept's currently active LLM profile for the Personal-KR Python
/// process.  The returned object is written to child stdin only; it must never
/// be placed on argv or logged because it can contain API/session credentials.
inline QJsonObject personal_kr_active_llm_config() {
    auto& llm = ai_chat::LlmService::instance();
    if (!llm.is_configured())
        return {};

    const QString provider = llm.active_provider().toLower();
    const QString model = llm.active_model();
    const int configured_max = llm.active_max_tokens();
    const int catalog_cap = ai_chat::ModelCatalog::output_cap(provider, model);
    const int resolved_max = configured_max > 0 ? (catalog_cap > 0 ? qMin(configured_max, catalog_cap) : configured_max)
                                                  : (catalog_cap > 0 ? catalog_cap : 8192);
    QJsonObject out{
        {"provider", provider},
        {"model_id", model},
        {"api_key", llm.active_api_key()},
        {"base_url", llm.active_base_url()},
        {"temperature", llm.active_temperature()},
        {"max_tokens", resolved_max},
    };

    // Fincept's first-party provider uses the authenticated terminal endpoint,
    // not an OpenAI-compatible public base URL.  Carry the live session token
    // through stdin alongside the API key so the Python research chain can use
    // the same account/provider the rest of the desktop app uses.
    if (provider == QLatin1String("fincept")) {
        out["endpoint"] = AppConfig::instance().api_base_url() + QStringLiteral("/research/chat");
        out["session_token"] = auth::AuthManager::instance().session().session_token;
    } else {
        const QString endpoint =
            ai_chat::ProviderCatalog::chat_endpoint(provider, llm.active_base_url(), model);
        if (!endpoint.isEmpty())
            out["endpoint"] = endpoint;
    }
    return out;
}

} // namespace fincept::services::equity
