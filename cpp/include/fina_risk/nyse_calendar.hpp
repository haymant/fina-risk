#pragma once

/// NYSE trading calendar -- the single C++ source of truth.
///
/// This block used to live inline in src/fina_risk_cpp.cpp. It was moved here
/// unchanged so that a test can reach it: there is a second, independent
/// implementation in src/fina_risk/daily_termsheet.py (us_market_holidays /
/// nyse_serials), and nothing previously compared the two. tests/test_nyse_
/// calendar.py now does, over 2015-2040.
///
/// Do NOT reformat the bodies when editing here; keep the Python copy in step
/// by hand and let the cross-language test prove they still agree. A silent
/// divergence in a holiday list is a silent change to every fixing count, and
/// therefore to every accrual fraction, on every affected day.

#include <vector>

namespace fina::risk::cal {

struct CivilDate {
    int year{};
    int month{};
    int day{};
};

inline CivilDate civil_from_serial(int serial) {
    // Howard Hinnant's days_from_civil inverse; z is days since 1970-01-01.
    const long z = static_cast<long>(serial) - 25569L + 719468L;
    const long era = (z >= 0 ? z : z - 146096L) / 146097L;
    const unsigned doe = static_cast<unsigned>(z - era * 146097L);
    const unsigned yoe = (doe - doe / 1460U + doe / 36524U - doe / 146096U) / 365U;
    const int year = static_cast<int>(yoe) + static_cast<int>(era) * 400;
    const unsigned doy = doe - (365U * yoe + yoe / 4U - yoe / 100U);
    const unsigned mp = (5U * doy + 2U) / 153U;
    const unsigned day = doy - (153U * mp + 2U) / 5U + 1U;
    const unsigned month = mp + (mp < 10U ? 3U : static_cast<unsigned>(-9));
    return {year + (month <= 2U ? 1 : 0), static_cast<int>(month), static_cast<int>(day)};
}

inline long days_from_civil(int year, unsigned month, unsigned day) {
    year -= month <= 2U;
    const long era = (year >= 0 ? year : year - 399) / 400;
    const unsigned yoe = static_cast<unsigned>(year - era * 400);
    const unsigned mp = month > 2U ? month - 3U : month + 9U;
    const unsigned doy = (153U * mp + 2U) / 5U + day - 1U;
    const unsigned doe = yoe * 365U + yoe / 4U - yoe / 100U + doy;
    return era * 146097L + static_cast<long>(doe) - 719468L;
}

inline int serial_from_ymd(int year, int month, int day) {
    return static_cast<int>(days_from_civil(year, static_cast<unsigned>(month), static_cast<unsigned>(day)) + 25569L);
}

inline int weekday_mon0(int serial) {  // 0=Mon .. 6=Sun (1970-01-01 was a Thursday)
    const long z = static_cast<long>(serial) - 25569L;
    return static_cast<int>(((z % 7) + 10) % 7);
}

inline int observed_weekday(int year, int month, int day) {
    const int serial = serial_from_ymd(year, month, day);
    const int w = weekday_mon0(serial);
    if (w == 5) return serial - 1;  // Saturday -> preceding Friday
    if (w == 6) return serial + 1;  // Sunday -> following Monday
    return serial;
}

inline int nth_weekday_of_month(int year, int month, int n, int target) {
    const int first = serial_from_ymd(year, month, 1);
    const int delta = (target - weekday_mon0(first) + 7) % 7;
    return first + delta + (n - 1) * 7;
}

inline int last_weekday_of_month(int year, int month, int target) {
    const int next_month = month == 12 ? 1 : month + 1;
    const int next_year = month == 12 ? year + 1 : year;
    const int last = serial_from_ymd(next_year, next_month, 1) - 1;
    return last - ((weekday_mon0(last) - target + 7) % 7);
}

inline int easter_sunday(int year) {
    // Anonymous Gregorian computus.
    const int a = year % 19, b = year / 100, c = year % 100, d = b / 4, e = b % 4;
    const int f = (b + 8) / 25, g = (b - f + 1) / 3, h = (19 * a + b - d - g + 15) % 30;
    const int i = c / 4, k = c % 4, l = (32 + 2 * e + 2 * i - h - k) % 7;
    const int m = (a + 11 * h + 22 * l) / 451;
    const int month = (h + l - 7 * m + 114) / 31;
    const int day = ((h + l - 7 * m + 114) % 31) + 1;
    return serial_from_ymd(year, month, day);
}

inline bool is_us_market_holiday(int serial) {
    const CivilDate c = civil_from_serial(serial);
    const int y = c.year;
    if (serial == observed_weekday(y, 1, 1)) return true;              // New Year's Day
    // When 1 January falls on a Saturday the exchange observes the holiday on
    // the PRECEDING Friday, which is 31 December of this year. Looking the
    // holiday up by the current date's own year never reaches it, so the NYSE
    // calendar used to trade 31 Dec of 2021, 2027, 2032 and 2038 -- a wrong
    // fixing count, and therefore a wrong accrual, on a real trading day.
    if (serial == observed_weekday(y + 1, 1, 1)) return true;
    if (serial == nth_weekday_of_month(y, 1, 3, 0)) return true;        // MLK Day
    if (serial == nth_weekday_of_month(y, 2, 3, 0)) return true;        // Washington's Birthday
    if (serial == easter_sunday(y) - 2) return true;                    // Good Friday
    if (serial == last_weekday_of_month(y, 5, 0)) return true;          // Memorial Day
    if (y >= 2022 && serial == observed_weekday(y, 6, 19)) return true; // Juneteenth
    if (serial == observed_weekday(y, 7, 4)) return true;               // Independence Day
    if (serial == nth_weekday_of_month(y, 9, 1, 0)) return true;        // Labor Day
    if (serial == nth_weekday_of_month(y, 11, 4, 3)) return true;       // Thanksgiving
    if (serial == observed_weekday(y, 12, 25)) return true;             // Christmas
    return false;
}

inline std::vector<int> nyse_schedule(int start, int end) {
    std::vector<int> dates;
    for (int serial = start; serial <= end; ++serial) {
        if (weekday_mon0(serial) <= 4 && !is_us_market_holiday(serial)) dates.push_back(serial);
    }
    return dates;
}

}  // namespace fina::risk::cal
