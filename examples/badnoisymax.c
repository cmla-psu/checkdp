int noisymax (float q[], int size, float epsilon)
{
  "TYPES: epsilon: <0, 0>; size: <0, 0>; q: <*, *>;";
  "PRECONDITION: ALL_DIFFER;";
  "CHECK: epsilon";
  int max = 0;
  int i = 0;
  float bq = 0;

  while(i < size)
  {
    float eta = Lap(2 / epsilon);

    if(q[i] + eta > bq || i == 0)
    {
      max = q[i] + eta;
      bq = q[i] + eta;
    }
    i = i + 1;
  }
  CHECKDP_OUTPUT(max);
}