#include "sora/types.hpp"

namespace sora {

namespace {
[[noreturn]] void bad(std::string_view what, std::string_view v) {
    throw Error("unknown " + std::string(what) + " '" + std::string(v) + "'");
}
}  // namespace

Stage parse_stage(std::string_view s) {
    if (s == "stage1") return Stage::S1;
    if (s == "stage2") return Stage::S2;
    if (s == "stage3") return Stage::S3;
    if (s == "poci") return Stage::Poci;
    if (s == "not_applicable") return Stage::NotApplicable;
    bad("stage", s);
}

ExposureType parse_exposure_type(std::string_view s) {
    if (s == "loan") return ExposureType::Loan;
    if (s == "debt_security") return ExposureType::DebtSecurity;
    if (s == "finance_lease") return ExposureType::FinanceLease;
    if (s == "loan_commitment") return ExposureType::LoanCommitment;
    if (s == "financial_guarantee") return ExposureType::FinancialGuarantee;
    if (s == "other_commitment") return ExposureType::OtherCommitment;
    bad("exposure_type", s);
}

Measurement parse_measurement(std::string_view s) {
    if (s == "amortised_cost") return Measurement::AmortisedCost;
    if (s == "fvoci") return Measurement::Fvoci;
    if (s == "fvoci_equity") return Measurement::FvociEquity;
    if (s == "fvtpl_mandatory") return Measurement::FvtplMandatory;
    if (s == "fvtpl_designated") return Measurement::FvtplDesignated;
    if (s == "held_for_trading") return Measurement::HeldForTrading;
    bad("measurement_category", s);
}

EbaSector parse_eba_sector(std::string_view s) {
    if (s == "central_bank") return EbaSector::CentralBank;
    if (s == "general_government") return EbaSector::GeneralGovernment;
    if (s == "credit_institution") return EbaSector::CreditInstitution;
    if (s == "other_financial") return EbaSector::OtherFinancial;
    if (s == "non_financial_corporation") return EbaSector::NonFinancialCorporation;
    if (s == "household") return EbaSector::Household;
    bad("eba_sector", s);
}

HouseholdPurpose parse_household_purpose(std::string_view s) {
    if (s.empty()) return HouseholdPurpose::None;
    if (s == "house_purchase") return HouseholdPurpose::HousePurchase;
    if (s == "consumption") return HouseholdPurpose::Consumption;
    if (s == "other") return HouseholdPurpose::Other;
    bad("household_purpose", s);
}

std::string_view to_string(Stage s) noexcept {
    switch (s) {
        case Stage::S1: return "stage1";
        case Stage::S2: return "stage2";
        case Stage::S3: return "stage3";
        case Stage::Poci: return "poci";
        case Stage::NotApplicable: return "not_applicable";
    }
    return "?";
}

std::string_view to_string(ExposureType t) noexcept {
    switch (t) {
        case ExposureType::Loan: return "loan";
        case ExposureType::DebtSecurity: return "debt_security";
        case ExposureType::FinanceLease: return "finance_lease";
        case ExposureType::LoanCommitment: return "loan_commitment";
        case ExposureType::FinancialGuarantee: return "financial_guarantee";
        case ExposureType::OtherCommitment: return "other_commitment";
    }
    return "?";
}

std::string_view to_string(Measurement m) noexcept {
    switch (m) {
        case Measurement::AmortisedCost: return "amortised_cost";
        case Measurement::Fvoci: return "fvoci";
        case Measurement::FvociEquity: return "fvoci_equity";
        case Measurement::FvtplMandatory: return "fvtpl_mandatory";
        case Measurement::FvtplDesignated: return "fvtpl_designated";
        case Measurement::HeldForTrading: return "held_for_trading";
    }
    return "?";
}

}  // namespace sora
