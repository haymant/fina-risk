// Block test: the NYSE trading calendar.
//
// This exists because the calendar is implemented twice -- once in C++
// (include/fina_risk/nyse_calendar.hpp, used by fina_risk_cpp.cpp) and once in
// Python (daily_termsheet.us_market_holidays / nyse_serials). Two hand-written
// holiday lists with nothing comparing them is a silent way to change every
// fixing count, and therefore every accrual fraction, on an affected day.
//
// This test does NOT hardcode an expected calendar. That would just be a third
// copy to keep in sync, and it would go stale the moment a real early-close or
// special-closure rule is added. Instead it asserts the properties that must
// hold for any sane trading calendar, and it dumps the schedule on stdout so
// tests/test_nyse_calendar.py can diff it day-for-day against the Python copy.
//
// Run:  ctest --test-dir build -R block/
// Dump: ./fina-risk-block-nyse-calendar 2015 2040

#include "fina_risk/nyse_calendar.hpp"

#include <cstdio>
#include <cstdlib>
#include <set>
#include <vector>

namespace {

int failures = 0;

void check(bool condition, const char* what) {
    if (!condition) {
        std::fprintf(stderr, "  FAIL %s\n", what);
        ++failures;
    }
}

}  // namespace

int main(int argc, char** argv) {
    using namespace fina::risk::cal;

    const int first_year = argc > 1 ? std::atoi(argv[1]) : 2015;
    const int last_year = argc > 2 ? std::atoi(argv[2]) : 2040;

    // ---------------------------------------------------------------
    // 1. Memorial Day is the LAST MONDAY of May, not the last weekday.
    //    This is the case that a "last weekday of the month" reading gets
    //    wrong in most years, so it is pinned explicitly.
    // ---------------------------------------------------------------
    for (int year = 2015; year <= 2040; ++year) {
        const int memorial = last_weekday_of_month(year, 5, 0);
        check(weekday_mon0(memorial) == 0, "Memorial Day must fall on a Monday");
        // The last Monday must be the final Monday: adding seven days must land
        // in June. Check via the serial arithmetic rather than civil dates.
        const int following = memorial + 7;
        const CivilDate c = civil_from_serial(following);
        check(c.month == 6, "the Monday after Memorial Day must be in June");
    }

    // ---------------------------------------------------------------
    // 2. Weekends are never trading days.
    // ---------------------------------------------------------------
    {
        const std::vector<int> days = nyse_schedule(serial_from_ymd(2026, 1, 1), serial_from_ymd(2027, 12, 31));
        for (const int serial : days) check(weekday_mon0(serial) <= 4, "schedule must not contain a weekend day");
    }

    // ---------------------------------------------------------------
    // 3. Every fixed-date US market holiday is absent, including each
    //    observed shift. New Year's, July 4th and Christmas are the ones
    //    that move to the adjacent weekday.
    // ---------------------------------------------------------------
    {
        const std::vector<int> days = nyse_schedule(serial_from_ymd(2015, 1, 1), serial_from_ymd(2040, 12, 31));
        const std::set<int> trading(days.begin(), days.end());
        for (int year = 2015; year <= 2040; ++year) {
            check(trading.count(observed_weekday(year, 1, 1)) == 0, "New Year's Day must not trade");
            check(trading.count(observed_weekday(year, 7, 4)) == 0, "Independence Day must not trade");
            check(trading.count(observed_weekday(year, 12, 25)) == 0, "Christmas must not trade");
            if (year >= 2022) check(trading.count(observed_weekday(year, 6, 19)) == 0, "Juneteenth must not trade");
            check(trading.count(nth_weekday_of_month(year, 1, 3, 0)) == 0, "MLK Day must not trade");
            check(trading.count(nth_weekday_of_month(year, 2, 3, 0)) == 0, "Washington's Birthday must not trade");
            check(trading.count(nth_weekday_of_month(year, 9, 1, 0)) == 0, "Labor Day must not trade");
            check(trading.count(nth_weekday_of_month(year, 11, 4, 3)) == 0, "Thanksgiving must not trade");
            check(trading.count(easter_sunday(year) - 2) == 0, "Good Friday must not trade");
        }
    }

    // ---------------------------------------------------------------
    // 4. Roughly 252 days a year. A wide band, deliberately: it catches a
    //    calendar that has lost or gained a whole month, without asserting
    //    a precise early-close count that would be wrong by design.
    // ---------------------------------------------------------------
    {
        for (int year = 2015; year <= 2040; ++year) {
            const std::vector<int> days = nyse_schedule(serial_from_ymd(year, 1, 1), serial_from_ymd(year, 12, 31));
            check(days.size() >= 248 && days.size() <= 254, "a year should have ~252 trading days");
        }
    }

    // ---------------------------------------------------------------
    // 5. The schedule is strictly increasing, and honours the range.
    // ---------------------------------------------------------------
    {
        const int lo = serial_from_ymd(2026, 1, 1);
        const int hi = serial_from_ymd(2027, 12, 31);
        const std::vector<int> days = nyse_schedule(lo, hi);
        check(!days.empty(), "schedule must not be empty");
        for (std::size_t i = 0; i < days.size(); ++i) {
            check(days[i] >= lo && days[i] <= hi, "every date must be inside the requested range");
            if (i > 0) check(days[i] > days[i - 1], "schedule must be strictly increasing");
        }
    }

    // ---------------------------------------------------------------
    // 6. Dump for the Python cross-check. One serial per line, so
    //    test_nyse_calendar.py can diff it without a parser.
    // ---------------------------------------------------------------
    {
        const int lo = serial_from_ymd(first_year, 1, 1);
        const int hi = serial_from_ymd(last_year, 12, 31);
        for (const int serial : nyse_schedule(lo, hi)) std::printf("%d\n", serial);
    }

    if (failures != 0) {
        std::fprintf(stderr, "%d calendar check(s) failed\n", failures);
        return 1;
    }
    std::fprintf(stderr, "nyse_calendar: all checks passed\n");
    return 0;
}
