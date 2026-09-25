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
    void expect(std::size_t col, duckdb_type t, int scale = -1) const;
    duckdb_data_chunk chunk_;
    std::size_t size_;
    std::vector<void*> data_;
    std::vector<std::uint64_t*> validity_;
    const std::vector<duckdb_type>& types_;
    const std::vector<std::uint8_t>& scales_;
};

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
