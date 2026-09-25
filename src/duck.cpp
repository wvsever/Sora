#include "sora/duck.hpp"

#include <memory>

namespace sora {

std::string sql_quote(std::string_view s) {
    std::string out = "'";
    for (char c : s) {
        if (c == '\'') out += '\'';
        out += c;
    }
    out += '\'';
    return out;
}

// ------------------------------------------------------------------------------------------ Chunk

Chunk::Chunk(duckdb_data_chunk chunk, const std::vector<duckdb_type>& types, const std::vector<std::uint8_t>& scales)
    : chunk_(chunk), size_(duckdb_data_chunk_get_size(chunk)), types_(types), scales_(scales) {
    data_.resize(types.size());
    validity_.resize(types.size());
    for (std::size_t c = 0; c < types.size(); ++c) {
        duckdb_vector v = duckdb_data_chunk_get_vector(chunk_, c);
        data_[c] = duckdb_vector_get_data(v);
        validity_[c] = duckdb_vector_get_validity(v);  // nullptr = all valid
    }
}

Chunk::~Chunk() { duckdb_destroy_data_chunk(&chunk_); }

bool Chunk::valid(std::size_t col, std::size_t row) const noexcept {
    return validity_[col] == nullptr || duckdb_validity_row_is_valid(validity_[col], row);
}

void Chunk::expect(std::size_t col, duckdb_type t, int scale) const {
    if (col >= types_.size() || types_[col] != t || (scale >= 0 && scales_[col] != scale)) {
        throw Error("column " + std::to_string(col) + ": unexpected type (engine query and SIM schema disagree)");
    }
}

std::string_view Chunk::str(std::size_t col, std::size_t row) const {
    expect(col, DUCKDB_TYPE_VARCHAR);
    auto* s = static_cast<duckdb_string_t*>(data_[col]) + row;
    const std::uint32_t len = s->value.inlined.length;
    return {len <= 12 ? s->value.inlined.inlined : s->value.pointer.ptr, len};
}

std::int64_t Chunk::i64(std::size_t col, std::size_t row) const {
    expect(col, DUCKDB_TYPE_BIGINT);
    return static_cast<const std::int64_t*>(data_[col])[row];
}

Date Chunk::date(std::size_t col, std::size_t row) const {
    expect(col, DUCKDB_TYPE_DATE);
    return static_cast<const std::int32_t*>(data_[col])[row];
}

bool Chunk::boolean(std::size_t col, std::size_t row) const {
    expect(col, DUCKDB_TYPE_BOOLEAN);
    return static_cast<const bool*>(data_[col])[row];
}

double Chunk::f64(std::size_t col, std::size_t row) const {
    expect(col, DUCKDB_TYPE_DOUBLE);
    return static_cast<const double*>(data_[col])[row];
}

Cents Chunk::cents(std::size_t col, std::size_t row) const {
    expect(col, DUCKDB_TYPE_DECIMAL, 2);   // DECIMAL(18,2): stored as int64 = cents
    return static_cast<const std::int64_t*>(data_[col])[row];
}

Nano Chunk::nano(std::size_t col, std::size_t row) const {
    expect(col, DUCKDB_TYPE_DECIMAL, 9);   // DECIMAL(18,9): stored as int64 = 1e-9 units
    return static_cast<const std::int64_t*>(data_[col])[row];
}

// ------------------------------------------------------------------------------------------ Duck

Duck::Duck(const DuckOptions& options) {
    duckdb_config config;
    if (duckdb_create_config(&config) != DuckDBSuccess) throw Error("duckdb: cannot create config");
    duckdb_set_config(config, "memory_limit", options.memory_limit.c_str());
    if (options.threads > 0) duckdb_set_config(config, "threads", std::to_string(options.threads).c_str());
    duckdb_set_config(config, "autoinstall_known_extensions", "false");
    duckdb_set_config(config, "autoload_known_extensions", "false");
    char* err = nullptr;
    const auto state = duckdb_open_ext(nullptr, &db_, config, &err);
    duckdb_destroy_config(&config);
    if (state != DuckDBSuccess) {
        std::string msg = err ? err : "unknown error";
        duckdb_free(err);
        throw Error("duckdb: cannot open: " + msg);
    }
    if (duckdb_connect(db_, &con_) != DuckDBSuccess) throw Error("duckdb: cannot connect");
}

Duck::~Duck() {
    if (con_) duckdb_disconnect(&con_);
    if (db_) duckdb_close(&db_);
}

void Duck::exec(const std::string& sql) {
    duckdb_result r;
    const auto state = duckdb_query(con_, sql.c_str(), &r);
    std::string err = state != DuckDBSuccess ? duckdb_result_error(&r) : "";
    duckdb_destroy_result(&r);
    if (state != DuckDBSuccess) throw Error("duckdb: " + err);
}

void Duck::query(const std::string& sql, const std::function<void(const Chunk&)>& on_chunk) {
    duckdb_prepared_statement stmt;
    if (duckdb_prepare(con_, sql.c_str(), &stmt) != DuckDBSuccess) {
        std::string err = duckdb_prepare_error(stmt);
        duckdb_destroy_prepare(&stmt);
        throw Error("duckdb: " + err + "\n  in: " + sql);
    }
    duckdb_pending_result pending;
    // Streaming execution: chunks are produced on demand instead of materialising the result.
    const auto pstate = duckdb_pending_prepared_streaming(stmt, &pending);
    duckdb_destroy_prepare(&stmt);
    if (pstate != DuckDBSuccess) {
        std::string err = duckdb_pending_error(pending);
        duckdb_destroy_pending(&pending);
        throw Error("duckdb: " + err);
    }
    duckdb_result result;
    const auto estate = duckdb_execute_pending(pending, &result);
    duckdb_destroy_pending(&pending);
    if (estate != DuckDBSuccess) {
        std::string err = duckdb_result_error(&result);
        duckdb_destroy_result(&result);
        throw Error("duckdb: " + err);
    }
    std::unique_ptr<duckdb_result, void (*)(duckdb_result*)> guard(&result, duckdb_destroy_result);

    const idx_t ncol = duckdb_column_count(&result);
    std::vector<duckdb_type> types(ncol);
    std::vector<std::uint8_t> scales(ncol, 0);
    for (idx_t c = 0; c < ncol; ++c) {
        duckdb_logical_type lt = duckdb_column_logical_type(&result, c);
        types[c] = duckdb_get_type_id(lt);
        if (types[c] == DUCKDB_TYPE_DECIMAL) {
            if (duckdb_decimal_internal_type(lt) != DUCKDB_TYPE_BIGINT) {
                duckdb_destroy_logical_type(&lt);
                throw Error("duckdb: DECIMAL column " + std::to_string(c) + " is not DECIMAL(18,x)");
            }
            scales[c] = duckdb_decimal_scale(lt);
        }
        duckdb_destroy_logical_type(&lt);
    }
    while (true) {
        duckdb_data_chunk dc = duckdb_fetch_chunk(result);
        if (!dc) break;
        Chunk chunk(dc, types, scales);
        if (chunk.size() == 0) break;
        on_chunk(chunk);
    }
    const char* err = duckdb_result_error(&result);
    if (err && *err) throw Error(std::string("duckdb: ") + err);
}

std::string Duck::scalar_string(const std::string& sql) {
    std::string out;
    bool seen = false;
    query(sql, [&](const Chunk& c) {
        if (!seen && c.size() > 0) {
            out = std::string(c.str_or(0, 0));
            seen = true;
        }
    });
    return out;
}

}  // namespace sora
