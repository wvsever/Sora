#pragma once
// JSON string escaping, shared by every hand-written JSON writer (summary.json, diagnostics.json, the calculator
// request encoder). Escapes exactly as nlohmann/json's dump() does: '"', '\\', the short forms \b \f \n \r \t,
// and every other control character below 0x20 as \u00xx (lower-case hex). Other bytes, including UTF-8
// sequences, are copied unchanged, so the calculator requests stay byte-identical to the nlohmann encoding.

#include <string>
#include <string_view>

namespace sora {

// Appends the escaped contents of `s` (without the surrounding quotes) to `out`.
inline void json_escape_to(std::string& out, std::string_view s) {
    static constexpr char hex[] = "0123456789abcdef";
    for (const char ch : s) {
        const auto c = static_cast<unsigned char>(ch);
        switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\b': out += "\\b"; break;
            case '\f': out += "\\f"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (c < 0x20) {
                    out += "\\u00";
                    out += hex[c >> 4];
                    out += hex[c & 15];
                } else {
                    out += ch;
                }
        }
    }
}

inline std::string json_escape(std::string_view s) {
    std::string out;
    out.reserve(s.size());
    json_escape_to(out, s);
    return out;
}

// `s` as a quoted JSON string.
inline std::string json_quote(std::string_view s) {
    std::string out = "\"";
    json_escape_to(out, s);
    out += '"';
    return out;
}

}  // namespace sora
