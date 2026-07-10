# Homepage scraping and batch labeling

This synthetic review run shows two API call shapes in one visible flow.

1. Fetch each homepage independently and persist its decoded response.
2. Collect ready homepage texts in bounded groups of six.
3. Send one simulated labeling request for each group at a cost of two units.
4. Persist every member result under its original domain key.
5. Continue successful members into a simulated labelled export while holding
   only the member that failed.

The source contains 24 generated websites. It performs zero network requests and
zero external writes. One homepage fetch and one batch member fail intentionally.
