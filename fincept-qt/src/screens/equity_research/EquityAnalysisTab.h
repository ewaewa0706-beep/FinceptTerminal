// src/screens/equity_research/EquityAnalysisTab.h
#pragma once
#include "services/equity/EquityResearchModels.h"
#include "ui/widgets/LoadingOverlay.h"

#include <QHash>
#include <QJsonArray>
#include <QJsonObject>
#include <QLabel>
#include <QVector>
#include <QWidget>

#include <array>
#include <cmath>
#include <cstdint>

class QFrame;
class QPlainTextEdit;
class QPushButton;
class QComboBox;
class QDateEdit;
class QDoubleSpinBox;
class QSpinBox;
class QTableWidget;

namespace fincept::screens {

// Custom-painted analyst price-target gauge (defined in the .cpp, no Q_OBJECT).
class AnalysisPriceTargetGauge;

// EquityAnalysisTab — the "Analysis" sub-tab of Equity Research.
//
// Unlike Overview (which lists raw fundamentals), this tab *interprets* the
// same StockInfo into decisions: an analyst price-target gauge plus six
// color-coded verdict cards. Those ratings are computed purely from StockInfo.
// Korean listings additionally expose an explicit on-demand AI research panel;
// it is user-triggered and does not change the lightweight default tab load.
class EquityAnalysisTab : public QWidget {
    Q_OBJECT
  public:
    explicit EquityAnalysisTab(QWidget* parent = nullptr);
    void set_symbol(const QString& symbol);

  private slots:
    void on_info_loaded(services::equity::StockInfo info);
    void on_kr_discover_clicked();
    void on_kr_discovery_research_clicked();
    void on_kr_discovery_batch_clicked();
    void on_kr_research_clicked();

  protected:
    void changeEvent(QEvent* event) override;

  private:
    // ── Verdict model ─────────────────────────────────────────────────────────
    enum class Tone : std::uint8_t { Good, Caution, Bad, Neutral, NA };

    struct Verdict {
        const char* rating_key = "—"; ///< QT_TR_NOOP literal chosen at assess time
        Tone tone = Tone::NA;
        QString line1;
        QString line2;
        QString line3;
        QString rationale; ///< already tr()'d at build time
    };

    // One verdict card's live labels (scaffolding built once in build_ui).
    struct VerdictCard {
        const char* title_key = nullptr;
        QLabel* rating = nullptr;
        std::array<QLabel*, 3> lines{nullptr, nullptr, nullptr};
        QLabel* rationale = nullptr;
    };

    enum Dim : std::uint8_t { kValuation, kHealth, kCashFlow, kProfitability, kGrowth, kRisk, kDimCount };

    // ── Build ───────────────────────────────────────────────────────────────
    void build_ui();
    QFrame* build_hero_();
    QFrame* build_kr_discovery_panel_();
    QFrame* build_kr_research_panel_();
    VerdictCard build_verdict_card_(QWidget* parent_grid_cell, const char* title_key, const QString& accent);
    void retranslateUi();

    QFrame* make_panel_(const char* title_key, const QString& accent_color);

    // ── Populate ──────────────────────────────────────────────────────────────
    void populate_hero_(const services::equity::StockInfo& info);
    void apply_verdict_(const VerdictCard& card, const Verdict& v);

    // ── Assessment (pure, from StockInfo) ──────────────────────────────────────
    Verdict assess_valuation_(const services::equity::StockInfo& s) const;
    Verdict assess_health_(const services::equity::StockInfo& s) const;
    Verdict assess_cashflow_(const services::equity::StockInfo& s) const;
    Verdict assess_profitability_(const services::equity::StockInfo& s) const;
    Verdict assess_growth_(const services::equity::StockInfo& s) const;
    Verdict assess_risk_(const services::equity::StockInfo& s) const;

    // ── Formatting ─────────────────────────────────────────────────────────────
    QString fmt(double v, int decimals = 2) const;
    static QString fmt_large(double v);
    QString fmt_pct(double v) const;   ///< v is a fraction (0.31 → "31.00%")
    QString fmt_money(double v) const; ///< currency-symbol prefixed price
    QString cur_symbol_() const;       ///< $/₹/€/£ from cached_info_.currency
    QString color_for_(Tone t) const;
    bool is_korean_symbol_() const;
    QString kr_ticker_() const;
    QString kr_market_() const;

    // ── State ──────────────────────────────────────────────────────────────────
    QHash<QLabel*, const char*> i18n_labels_; ///< static label → English source key
    services::equity::StockInfo cached_info_;
    bool info_loaded_ = false;
    QString current_symbol_;

    // Hero
    AnalysisPriceTargetGauge* gauge_ = nullptr;
    QLabel* hero_price_ = nullptr;  ///< "$182.40 now"
    QLabel* hero_upside_ = nullptr; ///< "+18.3% to mean target"
    QLabel* hero_reco_ = nullptr;   ///< recommendation badge
    QLabel* hero_count_ = nullptr;  ///< "34 analysts"
    QLabel* hero_empty_ = nullptr;  ///< "No analyst coverage" fallback

    // Verdict cards (one per Dim)
    std::array<VerdictCard, kDimCount> cards_{};

    // Personal Korean-market AI research. This is deliberately research-only:
    // it invokes the Python research engine and never routes to the order ticket.
    QFrame* kr_discovery_panel_ = nullptr;
    QPushButton* kr_discover_btn_ = nullptr;
    QPushButton* kr_discovery_research_btn_ = nullptr;
    QPushButton* kr_discovery_batch_btn_ = nullptr;
    QComboBox* kr_discovery_profile_ = nullptr;
    QComboBox* kr_discovery_market_ = nullptr;
    QDateEdit* kr_discovery_date_ = nullptr;
    QDoubleSpinBox* kr_discovery_min_value_ = nullptr;
    QSpinBox* kr_discovery_limit_ = nullptr;
    QLabel* kr_discovery_status_ = nullptr;
    QTableWidget* kr_discovery_table_ = nullptr;
    QPlainTextEdit* kr_discovery_result_ = nullptr;
    QJsonArray kr_discovery_candidates_;
    QJsonObject kr_discovery_ranking_;

    QFrame* kr_panel_ = nullptr;
    QPushButton* kr_research_btn_ = nullptr;
    QLabel* kr_status_ = nullptr;
    QPlainTextEdit* kr_result_ = nullptr;

    ui::LoadingOverlay* loading_overlay_ = nullptr;
};

} // namespace fincept::screens
