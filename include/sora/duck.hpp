#pragma once
// Thin RAII wrapper around the DuckDB C API. The engine uses DuckDB only to read SIM files
// (Parquet/CSV) with column projection. Results are streamed chunk by chunk (at most 2048 rows),
// so memory stays bounded whatever the table size.

#include <duckdb.h>

#include <cstdint>
#include <functional>
#include <string>
#include <string_view>
#include <vector>

#include "sora/types.hpp"

namespace sora {

struct DuckOptions {
    std::string memory_limit = "1GB";
    int threads = 0;  // 0 = DuckDB default (all cores)
    std::string temp_directory;   // where sorts beyond memory_limit spill; empty = DuckDB default (.tmp in the cwd)
};

// One chunk of a streamed result. Accessors are typed; a mismatch with the column type throws.
class Chunk {
public:
    explicit Chunk(duckdb_data_chunk chunk, const std::vector<duckdb_type>& types, const std::vector<std::uint8_t>& scales);
    Chunk(const Chunk&) = delete;
    Chunk& operator=(const Chunk&) = delete;
    ~Chunk();

    std::size_t size() const noexcept { return size_; }
    bool valid(std::size_t col, std::size_t row) const noexcept;
    std::string_view str(std::size_t col, std::size_t row) const;       // VARCHAR
    std::int64_t i64(std::size_t col, std::size_t row) const;           // BIGINT
    Date date(std::size_t col, std::size_t row) const;                  // DATE
    bool boolean(std::size_t col, std::size_t row) const;               // BOOLEAN
    double f64(std::size_t col, std::size_t row) const;                 // DOUBLE
    Cents cents(std::size_t col, std::size_t row) const;                // DECIMAL(18,2)
    Nano nano(std::size_t col, std::size_t row) const;                  // DECIMAL(18,9)

    // Nullable helpers.
    std::string_view str_or(std::size_t col, std::size_t row, std::string_view dflt = {}) const {
        return valid(col, row) ? str(col, row) : dflt;
    }
    Flag flag(std::size_t col, std::size_t row) const {
        return valid(col, row) ? (boolean(col, row) ? Flag::True : Flag::False) : Flag::Unknown;
    }

private:
    void expect(std::size_t col, duckdb_type t, int scale = -1) const {
        if (col >= types_.size() || types_[col] != t || (scale >= 0 && scales_[col] != scale)) type_error(col);
    }
    [[noreturn]] void type_error(std::size_t col) const;
    duckdb_data_chunk chunk_;
    std::size_t size_;
    std::vector<void*> data_;
    std::vector<std::uint64_t*> validity_;
    const std::vector<duckdb_type>& types_;
    const std::vector<std::uint8_t>& scales_;
};

// Accessors are inline: they run once per value on the load path (tens of millions of calls at 100x scale).
inline bool Chunk::valid(std::size_t col, std::size_t row) const noexcept {
    const std::uint64_t* v = validity_[col];   // nullptr = all valid; bit layout as duckdb_validity_row_is_valid
    return v == nullptr || ((v[row / 64] >> (row % 64)) & 1U) != 0;
}

inline std::string_view Chunk::str(std::size_t col, std::size_t row) const {
    expect(col, DUCKDB_TYPE_VARCHAR);
    auto* s = static_cast<duckdb_string_t*>(data_[col]) + row;
    const std::uint32_t len = s->value.inlined.length;
    return {len <= 12 ? s->value.inlined.inlined : s->value.pointer.ptr, len};
}

inline std::int64_t Chunk::i64(std::size_t col, std::size_t row) const {
    expect(col, DUCKDB_TYPE_BIGINT);
    return static_cast<const std::int64_t*>(data_[col])[row];
}

inline Date Chunk::date(std::size_t col, std::size_t row) const {
    expect(col, DUCKDB_TYPE_DATE);
    return static_cast<const std::int32_t*>(data_[col])[row];
}

inline bool Chunk::boolean(std::size_t col, std::size_t row) const {
    expect(col, DUCKDB_TYPE_BOOLEAN);
    return static_cast<const bool*>(data_[col])[row];
}

inline double Chunk::f64(std::size_t col, std::size_t row) const {
    expect(col, DUCKDB_TYPE_DOUBLE);
    return static_cast<const double*>(data_[col])[row];
}

inline Cents Chunk::cents(std::size_t col, std::size_t row) const {
    expect(col, DUCKDB_TYPE_DECIMAL, 2);   // DECIMAL(18,2): stored as int64 = cents
    return static_cast<const std::int64_t*>(data_[col])[row];
}

inline Nano Chunk::nano(std::size_t col, std::size_t row) const {
    expect(col, DUCKDB_TYPE_DECIMAL, 9);   // DECIMAL(18,9): stored as int64 = 1e-9 units
    return static_cast<const std::int64_t*>(data_[col])[row];
}

class Duck {
public:
    explicit Duck(const DuckOptions& options = {});
    Duck(const Duck&) = delete;
    Duck& operator=(const Duck&) = delete;
    ~Duck();

    void exec(const std::string& sql);
    // Streams the result of `sql`; `on_chunk` is called for every chunk in order.
    void query(const std::string& sql, const std::function<void(const Chunk&)>& on_chunk);
    // Convenience for single-value queries.
    std::string scalar_string(const std::string& sql);

private:
    duckdb_database db_ = nullptr;
    duckdb_connection con_ = nullptr;
};

// SQL-quoted string literal.
std::string sql_quote(std::string_view s);

}  // namespace sora
